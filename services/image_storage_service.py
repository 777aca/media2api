from __future__ import annotations

import hashlib
import hmac
import io
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlencode, urlparse

from curl_cffi import requests
from fastapi import HTTPException
from PIL import Image

from services.config import DATA_DIR, ConfigStore, config
from utils.log import logger

IMAGE_INDEX_FILE = DATA_DIR / "image_index.json"
IMAGE_INDEX_LOCK = Lock()
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_TIMEZONE = timezone(timedelta(hours=8))


class ImageStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredImage:
    rel: str
    url: str
    storage: str
    size: int


def _clean(value: object) -> str:
    return str(value or "").strip()


def _now_iso() -> str:
    return datetime.fromtimestamp(time.time(), IMAGE_TIMEZONE).isoformat(sep=" ", timespec="seconds")


def _image_instant(rel: str, created_at: object = None, modified_at: float | None = None) -> datetime | None:
    """Recover old image times without relying on the storage directory's date."""
    try:
        parsed = datetime.fromisoformat(str(created_at))
    except ValueError:
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed.astimezone(IMAGE_TIMEZONE)
    # Generated filenames preserve Unix seconds even for old remote-only images.
    prefix, separator, _ = Path(rel).name.partition("_")
    if separator and len(prefix) == 10 and prefix.isdigit():
        return datetime.fromtimestamp(int(prefix), IMAGE_TIMEZONE)
    if parsed is not None:
        # Legacy timestamps without a filename epoch used the server's local zone.
        return parsed.astimezone(IMAGE_TIMEZONE)
    if modified_at is not None:
        return datetime.fromtimestamp(modified_at, IMAGE_TIMEZONE)
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _image_dimensions(payload: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            return image.size
    except Exception:
        return None


def _is_image_rel(path: str) -> bool:
    try:
        safe_rel = _safe_relative_path(path)
    except HTTPException:
        return False
    return Path(safe_rel).suffix.lower() in IMAGE_EXTENSIONS


def _local_image_path(relative_path: str, images_dir: Path | None = None) -> Path:
    rel = _safe_relative_path(relative_path)
    root = (images_dir if images_dir is not None else config.images_dir).resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    return path


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_object(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


class WebDAVClient:
    def __init__(self, settings: dict[str, object]):
        self.url = _clean(settings.get("webdav_url")).rstrip("/")
        self.username = _clean(settings.get("webdav_username"))
        self.password = _clean(settings.get("webdav_password"))
        self.root_path = _clean(settings.get("webdav_root_path")).strip("/")
        self.session = requests.Session()

    def _auth_kwargs(self) -> dict[str, object]:
        return {"auth": (self.username, self.password)} if self.username or self.password else {}

    def _request(self, method: str, url: str, **kwargs):
        response = self.session.request(method, url, timeout=30, **self._auth_kwargs(), **kwargs)
        if response.status_code >= 400 and not (method == "MKCOL" and response.status_code in {405}):
            raise ImageStorageError(f"WebDAV {method} failed: HTTP {response.status_code}")
        return response

    def remote_url(self, rel: str = "") -> str:
        parts = [part for part in [self.root_path, _safe_relative_path(rel) if rel else ""] if part]
        encoded = "/".join(quote(part, safe="") for item in parts for part in item.split("/") if part)
        return f"{self.url}/{encoded}" if encoded else self.url

    def ensure_dirs(self, rel: str) -> None:
        parts = [part for part in [self.root_path, Path(_safe_relative_path(rel)).parent.as_posix()] if part and part != "."]
        current = self.url
        for item in "/".join(parts).split("/"):
            if not item:
                continue
            current = f"{current}/{quote(item, safe='')}"
            response = self.session.request("MKCOL", current, timeout=30, **self._auth_kwargs())
            if response.status_code in {201, 405}:
                continue
            if response.status_code >= 400:
                raise ImageStorageError(f"WebDAV MKCOL failed: HTTP {response.status_code}")

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        self.ensure_dirs(rel)
        url = self.remote_url(rel)
        self._request("PUT", url, data=payload, headers={"Content-Type": content_type})
        return url

    def get(self, rel: str) -> bytes:
        response = self._request("GET", self.remote_url(rel))
        return bytes(response.content)

    def delete(self, rel: str) -> bool:
        response = self.session.request("DELETE", self.remote_url(rel), timeout=30, **self._auth_kwargs())
        if response.status_code in {200, 202, 204, 404}:
            return response.status_code != 404
        raise ImageStorageError(f"WebDAV DELETE failed: HTTP {response.status_code}")

    def test(self) -> dict[str, object]:
        if not self.url:
            return {"ok": False, "status": 0, "error": "WebDAV URL is required"}
        if urlparse(self.url).scheme not in {"http", "https"}:
            return {"ok": False, "status": 0, "error": "invalid WebDAV URL"}
        test_rel = ".media2api_webdav_test.txt"
        try:
            self.put(test_rel, b"media2api webdav test\n", content_type="text/plain")
            self.delete(test_rel)
            return {"ok": True, "status": 200, "error": None}
        except ImageStorageError as exc:
            return {"ok": False, "status": 0, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc) or exc.__class__.__name__}
        finally:
            self.session.close()


class S3ImageClient:
    def __init__(self, settings: dict[str, object]):
        self.account_id = _clean(settings.get("s3_account_id"))
        raw_endpoint = _clean(settings.get("s3_endpoint")).rstrip("/")
        self.bucket = _clean(settings.get("s3_bucket")).strip("/")
        if raw_endpoint:
            if self.bucket and raw_endpoint.endswith(f"/{self.bucket}"):
                raw_endpoint = raw_endpoint[: -len(self.bucket) - 1]
            self.endpoint = raw_endpoint
        elif self.account_id:
            self.endpoint = f"https://{self.account_id}.r2.cloudflarestorage.com"
        else:
            self.endpoint = ""
        self.region = _clean(settings.get("s3_region")) or "auto"
        self.access_key_id = _clean(settings.get("s3_access_key_id"))
        self.secret_access_key = _clean(settings.get("s3_secret_access_key"))
        if "s3_prefix" in settings and settings.get("s3_prefix") is not None:
            self.prefix = _clean(settings.get("s3_prefix")).strip("/")
        else:
            self.prefix = "media2api/images"
        self.session = requests.Session()

    def object_key(self, rel: str = "") -> str:
        safe_rel = _safe_relative_path(rel) if rel else ""
        parts = [part for part in [self.prefix, safe_rel] if part]
        return "/".join(parts)

    def remote_url(self, rel: str = "") -> str:
        key = self.object_key(rel)
        encoded_key = "/".join(quote(part, safe="") for part in key.split("/") if part)
        base = f"{self.endpoint}/{quote(self.bucket, safe='')}" if self.bucket else self.endpoint
        return f"{base}/{encoded_key}" if encoded_key else base

    def _aws_v4_headers(
        self,
        method: str,
        canonical_uri: str,
        *,
        query: dict[str, str] | None = None,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        now = _utc_now()
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        encoded_query = urlencode(sorted((query or {}).items()))
        payload_hash = _sha256_hex(body)
        parsed = urlparse(self.endpoint)
        host = parsed.netloc
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if extra_headers:
            for key, value in extra_headers.items():
                headers[key.lower()] = value.strip()
        sorted_items = sorted((key.lower(), " ".join(str(value).strip().split())) for key, value in headers.items())
        canonical_headers = "".join(f"{key}:{value}\n" for key, value in sorted_items)
        signed_headers = ";".join(key for key, _ in sorted_items)
        canonical_request = "\n".join([
            method.upper(),
            canonical_uri,
            encoded_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ])
        k_date = _hmac_sha256(("AWS4" + self.secret_access_key).encode("utf-8"), date_stamp)
        k_region = hmac.new(k_date, self.region.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, b"s3", hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        request_headers = {key: value for key, value in headers.items()}
        request_headers["authorization"] = authorization
        return encoded_query, request_headers

    def _request(
        self,
        method: str,
        rel: str = "",
        *,
        body: bytes = b"",
        extra_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ):
        parsed = urlparse(self.endpoint)
        base_path = parsed.path.rstrip("/")
        key = self.object_key(rel)
        encoded_key = "/".join(quote(part, safe="") for part in key.split("/") if part)
        canonical_uri = f"{base_path}/{quote(self.bucket, safe='')}"
        if encoded_key:
            canonical_uri += f"/{encoded_key}"
        encoded_query, headers = self._aws_v4_headers(
            method,
            canonical_uri,
            body=body,
            extra_headers=extra_headers,
        )
        url = f"{parsed.scheme}://{parsed.netloc}{canonical_uri}"
        if encoded_query:
            url += f"?{encoded_query}"
        return self.session.request(method.upper(), url, headers=headers, data=body, timeout=timeout)

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        response = self._request("PUT", rel, body=payload, extra_headers={"Content-Type": content_type})
        if response.status_code >= 400:
            raise ImageStorageError(f"R2/S3 PUT failed: HTTP {response.status_code}")
        return self.remote_url(rel)

    def get(self, rel: str) -> bytes:
        response = self._request("GET", rel)
        if response.status_code >= 400:
            raise ImageStorageError(f"R2/S3 GET failed: HTTP {response.status_code}")
        return bytes(response.content)

    def delete(self, rel: str) -> bool:
        try:
            response = self._request("DELETE", rel)
        except requests.RequestsError as exc:
            raise ImageStorageError("R2/S3 DELETE failed: network request failed") from exc
        if response.status_code in {200, 202, 204, 404}:
            return response.status_code != 404
        raise ImageStorageError(f"R2/S3 DELETE failed: HTTP {response.status_code}")

    def test(self) -> dict[str, object]:
        if not self.endpoint:
            return {"ok": False, "status": 0, "error": "Account ID or S3 Endpoint is required"}
        if urlparse(self.endpoint).scheme not in {"http", "https"}:
            return {"ok": False, "status": 0, "error": "invalid S3 Endpoint"}
        if not self.access_key_id or not self.secret_access_key:
            return {"ok": False, "status": 0, "error": "Access Key ID and Secret Access Key are required"}
        if not self.bucket:
            return {"ok": False, "status": 0, "error": "Bucket is required"}
        test_rel = ".media2api_s3_test.txt"
        try:
            self.put(test_rel, b"media2api s3 test\n", content_type="text/plain")
            self.delete(test_rel)
            return {"ok": True, "status": 200, "error": None}
        except ImageStorageError as exc:
            return {"ok": False, "status": 0, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc) or exc.__class__.__name__}
        finally:
            self.session.close()


class ImageStorageService:
    def __init__(self, index_file: Path = IMAGE_INDEX_FILE):
        self.index_file = index_file
        self._index_lock = IMAGE_INDEX_LOCK

    def settings(self) -> dict[str, object]:
        return config.get_image_storage_settings()

    def provider(self) -> str:
        settings = self.settings()
        raw_provider = _clean(settings.get("provider")).lower()
        if raw_provider in {"s3", "r2", "cloudflare_r2"}:
            return "s3"
        if raw_provider == "webdav":
            return "webdav"
        raw_mode = _clean(settings.get("mode")).lower()
        if raw_mode in {"s3", "r2", "cloudflare_r2"} or (
            _clean(settings.get("s3_bucket")) and not _clean(settings.get("webdav_url"))
        ):
            return "s3"
        return "webdav"

    def mode(self) -> str:
        raw_mode = _clean(self.settings().get("mode")).lower() or "local"
        if raw_mode in {"r2", "cloudflare_r2"}:
            return "s3"
        return raw_mode if raw_mode in {"local", "webdav", "s3", "both"} else "local"

    def _load_index(self) -> dict[str, dict[str, object]]:
        raw = _read_json_object(self.index_file)
        items = raw.get("items")
        if not isinstance(items, dict):
            return {}
        return {str(key): value for key, value in items.items() if isinstance(value, dict)}

    def _load_clean_index(self) -> dict[str, dict[str, object]]:
        items = self._load_index()
        return {rel: item for rel, item in items.items() if _is_image_rel(rel)}

    def _save_index(self, items: dict[str, dict[str, object]]) -> None:
        _write_json_object(self.index_file, {"items": items})

    def _public_url(self, rel: str, base_url: str | None = None) -> str:
        settings = self.settings()
        safe_rel = _safe_relative_path(rel)
        public_base_url = _clean(settings.get("public_base_url")).rstrip("/")
        if public_base_url:
            if self.provider() == "s3":
                if "s3_prefix" in settings and settings.get("s3_prefix") is not None:
                    prefix = _clean(settings.get("s3_prefix")).strip("/")
                else:
                    prefix = "media2api/images"
                parsed_path = urlparse(public_base_url).path.strip("/")
                if prefix and not parsed_path:
                    return f"{public_base_url}/{prefix}/{safe_rel}"
            return f"{public_base_url}/{safe_rel}"
        return f"{(base_url or config.base_url).rstrip('/')}/images/{safe_rel}"

    def make_relative_path(self, image_data: bytes) -> str:
        file_hash = hashlib.md5(image_data).hexdigest()
        filename = f"{int(time.time())}_{file_hash}.png"
        relative_dir = Path(time.strftime("%Y"), time.strftime("%m"), time.strftime("%d"))
        return f"{relative_dir.as_posix()}/{filename}"

    def save(self, image_data: bytes, base_url: str | None = None) -> StoredImage:
        self.cleanup_expired_images()
        rel = self.make_relative_path(image_data)
        mode = self.mode()
        if mode not in {"local", "webdav", "s3", "both"}:
            mode = "local"
        stored_local = False
        stored_webdav = False
        stored_s3 = False
        remote_url = ""

        if mode in {"local", "both"}:
            path = _local_image_path(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(image_data)
            stored_local = True

        if mode in {"webdav", "s3", "both"}:
            if self.provider() == "s3":
                remote_url = S3ImageClient(self.settings()).put(rel, image_data)
                stored_s3 = True
            else:
                remote_url = WebDAVClient(self.settings()).put(rel, image_data)
                stored_webdav = True

        remote_kind = "s3" if stored_s3 else ("webdav" if stored_webdav else "")
        storage_label = "both" if stored_local and remote_kind else (remote_kind or "local")
        dimensions = _image_dimensions(image_data)
        created_at = _now_iso()
        item = {
            "rel": rel,
            "path": rel,
            "name": Path(rel).name,
            "date": created_at[:10],
            "size": len(image_data),
            "created_at": created_at,
            "storage": storage_label,
            "local": stored_local,
            "webdav": stored_webdav,
            "s3": stored_s3,
            "remote_url": remote_url,
        }
        if dimensions:
            item["width"], item["height"] = dimensions
        with self._index_lock:
            items = self._load_clean_index()
            items[rel] = item
            self._save_index(items)
        return StoredImage(rel=rel, url=self._public_url(rel, base_url), storage=storage_label, size=len(image_data))

    def get_bytes(self, rel: str) -> bytes:
        safe_rel = _safe_relative_path(rel)
        if not _is_image_rel(safe_rel):
            raise HTTPException(status_code=404, detail="image not found")
        path = _local_image_path(safe_rel)
        if path.is_file():
            return path.read_bytes()
        item = self._load_clean_index().get(safe_rel, {})
        if item.get("s3"):
            return S3ImageClient(self.settings()).get(safe_rel)
        if item.get("webdav"):
            return WebDAVClient(self.settings()).get(safe_rel)
        raise HTTPException(status_code=404, detail="image not found")

    def exists(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        if not _is_image_rel(safe_rel):
            return False
        if _local_image_path(safe_rel).is_file():
            return True
        item = self._load_clean_index().get(safe_rel, {})
        return bool(item.get("webdav") or item.get("s3"))

    def has_local(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        return _is_image_rel(safe_rel) and _local_image_path(safe_rel).is_file()

    def list_items(self, base_url: str, start_date: str = "", end_date: str = "") -> list[dict[str, object]]:
        with self._index_lock:
            indexed = self._load_clean_index()
            root = config.images_dir
            changed = False
            for path in root.rglob("*"):
                if not path.is_file() or not _is_image_rel(path.name):
                    continue
                rel = path.relative_to(root).as_posix()
                if rel in indexed:
                    continue
                dimensions = None
                try:
                    dimensions = _image_dimensions(path.read_bytes())
                except Exception:
                    dimensions = None
                instant = _image_instant(rel, modified_at=path.stat().st_mtime)
                indexed[rel] = {
                    "rel": rel,
                    "path": rel,
                    "name": path.name,
                    "date": instant.strftime("%Y-%m-%d"),
                    "size": path.stat().st_size,
                    "created_at": instant.isoformat(sep=" ", timespec="seconds"),
                    "storage": "local",
                    "local": True,
                    "webdav": False,
                    "s3": False,
                    **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
                }
                changed = True

            items: list[dict[str, object]] = []
            for rel, item in list(indexed.items()):
                if not _is_image_rel(rel):
                    indexed.pop(rel, None)
                    changed = True
                    continue
                local = _local_image_path(rel).is_file()
                webdav = bool(item.get("webdav"))
                s3 = bool(item.get("s3"))
                if not local and not webdav and not s3:
                    indexed.pop(rel, None)
                    changed = True
                    continue
                remote_kind = "s3" if s3 else ("webdav" if webdav else "")
                storage = "both" if local and remote_kind else (remote_kind or "local")
                if item.get("local") != local or item.get("storage") != storage:
                    item = {
                        **item,
                        "local": local,
                        "storage": storage,
                    }
                    indexed[rel] = item
                    changed = True
                instant = _image_instant(rel, item.get("created_at"))
                day = instant.strftime("%Y-%m-%d") if instant else str(item.get("date") or "")
                if start_date and day < start_date:
                    continue
                if end_date and day > end_date:
                    continue
                items.append({
                    **item,
                    "date": day,
                    "created_at": instant.strftime("%Y-%m-%d %H:%M:%S") if instant else item.get("created_at", ""),
                    "rel": rel,
                    "path": rel,
                    "url": self._public_url(rel, base_url),
                })
            if changed:
                self._save_index(indexed)
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return items

    def delete(self, rel: str, *, include_webdav: bool = True, config_store: ConfigStore | None = None) -> bool:
        safe_rel = _safe_relative_path(rel)
        store = config_store if config_store is not None else config
        path = _local_image_path(safe_rel, store.images_dir)
        with self._index_lock:
            items = self._load_clean_index()
            item = dict(items.get(safe_rel, {}))
            removed = False
            # Keep the index and local file until remote deletion succeeds, so a
            # failed DELETE (including a network timeout) can be retried.
            if item.get("s3"):
                S3ImageClient(store.get_image_storage_settings()).delete(safe_rel)
                item["s3"] = False
                removed = True  # A remote 404 also completes deletion of the record.
            if include_webdav and item.get("webdav"):
                WebDAVClient(store.get_image_storage_settings()).delete(safe_rel)
                item["webdav"] = False
                removed = True
            if path.is_file():
                path.unlink(missing_ok=True)
                removed = True
            if safe_rel in items:
                if item.get("webdav"):
                    items[safe_rel] = {**item, "local": False, "storage": "webdav"}
                else:
                    items.pop(safe_rel, None)
                self._save_index(items)
        return removed

    def cleanup_expired_images(self, *, config_store: ConfigStore | None = None) -> int:
        """Expire local files and indexed R2/S3 copies; retain WebDAV copies."""
        store = config_store if config_store is not None else config
        cutoff = time.time() - store.image_retention_hours * 3600
        root = store.images_dir.resolve()
        with self._index_lock:
            indexed = self._load_clean_index()
        candidates = {rel: item for rel, item in indexed.items() if item.get("s3")}
        for path in root.rglob("*"):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                candidates[rel] = indexed.get(rel, {})

        removed = 0
        for rel, item in candidates.items():
            try:
                unresolved = root / rel
                if unresolved.is_symlink() or any(
                    parent.is_symlink() for parent in unresolved.parents if parent.is_relative_to(root)
                ):
                    continue
                path = _local_image_path(rel, root)
                # Existing local files keep their original mtime-based policy.
                # Remote-only copies use their indexed creation time, falling
                # back to the Unix timestamp in legacy generated filenames.
                modified_at = path.stat().st_mtime if path.is_file() else None
                instant = _image_instant(rel, item.get("created_at")) if modified_at is None else None
                saved_at = modified_at if modified_at is not None else (instant.timestamp() if instant else None)
                if saved_at is None or saved_at > cutoff:
                    continue
                if self.delete(rel, include_webdav=False, config_store=store):
                    removed += 1
            except Exception as exc:
                # Continue with other images and retain failed records for the
                # next sweep. Do not log request URLs or storage credentials.
                logger.warning({"event": "image_retention_delete_failed", "path": rel,
                                "error_type": type(exc).__name__})

        for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            try:
                path.rmdir()
            except OSError:
                pass
        return removed

    def sync_all(self) -> dict[str, int]:
        settings = self.settings()
        provider = self.provider()
        if self.mode() not in {"webdav", "s3", "both"}:
            raise ImageStorageError("远程图片存储未启用")
        uploaded = 0
        skipped = 0
        failed = 0
        with self._index_lock:
            items = self._load_clean_index()
            client = S3ImageClient(settings) if provider == "s3" else WebDAVClient(settings)
            remote_flag = "s3" if provider == "s3" else "webdav"
            for path in sorted(config.images_dir.rglob("*")):
                if not path.is_file() or not _is_image_rel(path.name):
                    continue
                rel = path.relative_to(config.images_dir).as_posix()
                item = items.get(rel, {})
                if item.get(remote_flag):
                    skipped += 1
                    continue
                try:
                    payload = path.read_bytes()
                    remote_url = client.put(rel, payload)
                    dimensions = _image_dimensions(payload)
                    instant = _image_instant(rel, item.get("created_at"), path.stat().st_mtime)
                    items[rel] = {
                        **item,
                        "rel": rel,
                        "path": rel,
                        "name": path.name,
                        "date": instant.strftime("%Y-%m-%d"),
                        "size": len(payload),
                        "created_at": instant.isoformat(sep=" ", timespec="seconds"),
                        "storage": "both",
                        "local": True,
                        "webdav": bool(item.get("webdav")) or remote_flag == "webdav",
                        "s3": bool(item.get("s3")) or remote_flag == "s3",
                        "remote_url": remote_url,
                        **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
                    }
                    uploaded += 1
                except Exception:
                    failed += 1
            self._save_index(items)
        return {"uploaded": uploaded, "skipped": skipped, "failed": failed}

    def test_webdav(self) -> dict[str, object]:
        if self.provider() == "s3":
            return S3ImageClient(self.settings()).test()
        return WebDAVClient(self.settings()).test()

    def test_connection(self) -> dict[str, object]:
        return self.test_webdav()


image_storage_service = ImageStorageService()
