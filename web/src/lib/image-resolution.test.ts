import assert from "node:assert/strict";
import test from "node:test";
import { IMAGE_SIZE_PRESETS, imageSizeError, imageProcessingLabel, isExperimentalSize, parseImageProcessing } from "./image-resolution";

test("all presets meet official size constraints", () => {
  for (const preset of IMAGE_SIZE_PRESETS) assert.equal(imageSizeError(preset.ratio === "auto" ? "auto" : `${preset.width}x${preset.height}`), null, preset.label);
  assert.equal(imageSizeError("1024x640"), null);
  assert.equal(imageSizeError("2880x2880"), null);
});

test("rejects invalid custom dimensions and accepts auto", () => {
  for (const size of ["1365x1024", "3840x3840", "1024x512", "3072x768", "0x1024", "NaNx1024", "4096x2160"]) assert.ok(imageSizeError(size), size);
  assert.equal(imageSizeError("auto"), null);
  assert.equal(isExperimentalSize("2560", "1440"), false);
  assert.equal(isExperimentalSize("3840", "2160"), true);
});

test("processing metadata validates unknown fields and preserves legacy labels", () => {
  assert.deepEqual(parseImageProcessing({ actual_size: false, source_size: "bad", processing: "native_4k" }), {});
  assert.equal(imageProcessingLabel(parseImageProcessing({})), "");
  assert.equal(imageProcessingLabel(parseImageProcessing({ processing: "super_resolution" })), "AI 超分");
  assert.equal(imageProcessingLabel(parseImageProcessing({ processing: "none", processing_status: "fallback" })), "超分未完成，已保留原图");
  assert.deepEqual(parseImageProcessing({ actual_size: "1152x1152", requested_size: "2048x1152" }), { actual_size: "1152x1152", requested_size: "2048x1152" });
});
