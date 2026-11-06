import { ImageIcon, Layers, MessageSquareText, Presentation, Video } from "lucide-react";
import type { AuthRole } from "@/store/auth";

export const creativeRoutes = [
  { href: "/chat", label: "对话", tabLabel: "对话", icon: MessageSquareText, adminOnly: false },
  { href: "/image", label: "图片创作", tabLabel: "图片", icon: ImageIcon, adminOnly: false },
  { href: "/video", label: "视频创作", tabLabel: "视频", icon: Video, adminOnly: false },
  { href: "/ppt", label: "PPT 生成", tabLabel: "PPT", icon: Presentation, adminOnly: true },
  { href: "/psd", label: "PSD 生成", tabLabel: "PSD", icon: Layers, adminOnly: true },
];

export function getCreativeRoutes(role?: AuthRole) {
  return creativeRoutes.filter(route => !route.adminOnly || role === "admin");
}
