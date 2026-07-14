"""Vision tool — sense_image + register_attachment。

设计：vision 作为独立工具，主模型（文本）不感知多模态。
  - sense_image(attachment_id, question): 拉 attachment 字节 → base64 → 调 vision 模型 → 返回中文描述
  - register_attachment(path, description?): 把本地图片登记为 attachment，便于 sense_image 查看

vision 模型可配置（app.yaml model.vision，默认 glm-4.6v）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Dict

import httpx

from interfaces.tools import registry
from infrastructure.config import get_app_instance_id

logger = logging.getLogger(__name__)


# ── vision 模型配置读取 ───────────────────────────────────────────────────────


def _get_vision_model() -> str:
    """读取配置的 vision 模型，default glm-4.6v。"""
    try:
        iid = get_app_instance_id() or ""
        if not iid:
            return "glm-4.6v"
        from infrastructure.config import get_project_root
        import yaml as _yaml
        cfg_path = get_project_root() / "apps" / iid / "config" / "app.yaml"
        if cfg_path.exists():
            cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            model_cfg = cfg.get("model") or {}
            return (model_cfg.get("vision") or "").strip() or "glm-4.6v"
    except Exception:
        pass
    return "glm-4.6v"


def _get_llm_base_url() -> str:
    """复用主模型的 base_url（vision + 主模型同平台、同账号体系）。"""
    try:
        iid = get_app_instance_id() or ""
        if iid:
            from infrastructure.config import get_project_root
            import yaml as _yaml
            cfg_path = get_project_root() / "apps" / iid / "config" / "app.yaml"
            if cfg_path.exists():
                cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                model_cfg = cfg.get("model") or {}
                url = (model_cfg.get("base_url") or "").strip()
                if url:
                    return url.rstrip("/")
    except Exception:
        pass
    return "https://open.bigmodel.cn/api/paas/v4"


def _get_llm_api_key() -> str:
    """复用主模型的 API key。"""
    import os
    # 从 secrets.env 读（和主模型一致）
    try:
        iid = get_app_instance_id() or ""
        if iid:
            from infrastructure.config import get_project_root
            secrets_path = get_project_root() / "apps" / iid / "config" / "secrets.env"
            if secrets_path.exists():
                for line in secrets_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("LLM_API_KEY=") or line.startswith("GLM_API_KEY="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return os.getenv("LLM_API_KEY", "") or os.getenv("GLM_API_KEY", "")


# ── MIME 探测 ─────────────────────────────────────────────────────────────────


def _probe_mime(path: Path) -> str:
    """探测文件 MIME 类型（用 +++imghdr+++ 或 mimetypes）。"""
    try:
        import imghdr
        kind = imghdr.what(str(path))
        if kind:
            return f"image/{kind}"
    except Exception:
        pass
    # fallback: mimetypes
    import mimetypes
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


# ── vision LLM 调用 ───────────────────────────────────────────────────────────


def _call_vision_llm(
    model: str, data_uri: str, question: str, *, timeout: float = 120.0,
) -> str:
    """调 GLM vision 模型，OpenAI 兼容协议。

    Args:
        model: 模型名（glm-4.6v / glm-5v-turbo 等）
        data_uri: `data:image/jpeg;base64,/9j/...`
        question: 用户提问

    Returns:
        vision 模型的中文描述（str）。
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": question},
        ]}],
        "max_tokens": 800,
    }
    base_url = _get_llm_base_url()
    api_key = _get_llm_api_key()
    if not api_key:
        raise RuntimeError("LLM_API_KEY 未配置（vision 工具需要和主模型同一个 API key）")

    r = httpx.post(
        f"{base_url}/chat/completions",
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"vision 调用无 choices 响应: {data}")
    return choices[0].get("message", {}).get("content", "").strip()


# ── sense_image handler ───────────────────────────────────────────────────────


def _handle_sense_image(args: Dict[str, Any], **kwargs) -> str:
    """sense_image —— 查看附件图片并提问，返回视觉模型中文描述。"""
    att_id = (args.get("attachment_id") or "").strip()
    if not att_id:
        return registry.tool_error("必须传 attachment_id（如 feishu:img_v3_xxx）")
    question = (args.get("question") or "").strip() or "详细描述这张图片的内容"

    # 1. 查 attachment
    from infrastructure.persistence.instance.attachments import get_attachment
    att = get_attachment(att_id)
    if not att:
        return registry.tool_error(f"附件 {att_id} 不存在或已过期")

    # 2. 仅 image mime 才走 vision
    if not att.mime.startswith("image/"):
        return registry.tool_error(
            f"该附件是 {att.mime}（{att.kind}），sense_image 仅支持图片。"
            f"音频/文件请用其它方式处理（如 OCR / transcript）。"
        )

    # 3. 读 bytes → base64 data URI
    try:
        p = Path(att.local_path)
        if not p.is_file():
            return registry.tool_error(f"附件文件不存在（可能已清掉）: {att.local_path}")
        img_bytes = p.read_bytes()
    except Exception as exc:
        return registry.tool_error(f"读取附件失败: {exc}")

    # 大小限制：GLM 单图 ≤ 10MB（data URI 会 ≈ 1.33x 原始）
    if len(img_bytes) > 10 * 1024 * 1024:
        return registry.tool_error(
            f"图片太大（{len(img_bytes) // 1024 // 1024}MB），超过 GLM vision 上限 10MB。"
            f"可考虑先压缩或裁剪后用 register_attachment 再 sense_image。"
        )
    data_uri = f"data:{att.mime};base64,{base64.b64encode(img_bytes).decode()}"

    # 4. 调 vision 模型
    vision_model = _get_vision_model()
    try:
        desc = _call_vision_llm(vision_model, data_uri, question)
        return desc
    except Exception as exc:
        return registry.tool_error(
            f"vision 调用失败（model={vision_model}, size={len(img_bytes)}B）: {exc}"
        )


# ── register_attachment handler ───────────────────────────────────────────────


def _handle_register_attachment(args: Dict[str, Any], **kwargs) -> str:
    """register_attachment —— 把本地图片文件登记为附件。

    模型经常用 terminal / execute_code 下载或 matplotlib / pyecharts 生成图，
    登记到 attachment registry 才能用 sense_image 查看。
    """
    path_str = (args.get("path") or "").strip()
    if not path_str:
        return registry.tool_error("必须传 path（本地图片绝对路径）")
    p = Path(path_str).expanduser()
    if not p.is_file():
        return registry.tool_error(f"文件不存在: {p}")

    description = (args.get("description") or "").strip()

    iid = get_app_instance_id() or ""
    if not iid:
        return registry.tool_error("无法确定当前实例 ID（ContextVar 未设）")

    # 读 bytes + 探测 mime
    try:
        data = p.read_bytes()
    except Exception as exc:
        return registry.tool_error(f"读取文件失败: {exc}")
    mime = _probe_mime(p)
    sha = hashlib.sha256(data).hexdigest()

    # 落盘到 attachments/ 目录（按 sha256 去重——同图可以多 source_key 复用同一文件）
    from infrastructure.persistence.instance.attachments import (
        attachments_dir, ext_from_mime, register_attachment,
    )
    ext = ext_from_mime(mime)
    dest = attachments_dir(iid) / f"{sha[:16]}.{ext}"
    if not dest.exists() and str(p.resolve()) != str(dest.resolve()):
        try:
            shutil.copy2(p, dest)
        except Exception as exc:
            return registry.tool_error(f"复制到附件目录失败: {exc}")

    # register（source_key = local:{sha[:12]} 避免不同路径但同图冲突）
    source_key = f"local:{sha[:12]}"
    att = register_attachment(
        instance_id=iid, source="local", source_key=source_key,
        mime=mime, local_path=str(dest), size_bytes=len(data), sha256=sha,
    )

    note_extra = f"（{description}）" if description else ""
    return json.dumps({
        "ok": True,
        "attachment_id": att.attachment_id,
        "mime": mime,
        "size_bytes": len(data),
        "note": f"附件已登记{note_extra}。调 sense_image(attachment_id=\"{att.attachment_id}\") "
                f"可让 vision 模型查看并生成描述。",
    }, ensure_ascii=False)


# ── registry 注册 ─────────────────────────────────────────────────────────────


registry.register(
    name="sense_image",
    toolset="actions",
    schema={
        "name": "sense_image",
        "description": (
            "查看一张图片附件并提问，返回视觉模型（如 glm-4.6v）生成的中文描述。\n"
            "\n"
            "什么时候调：\n"
            "  - wake prompt / chat_stream / 新消息里出现 `[图片 xxx]` 时——不要凭 ID 猜图内容，主动调取。\n"
            "  - 自己用 matplotlib 等生成了图，调 register_attachment 登记后用本工具查看效果。\n"
            "\n"
            "参数建议：\n"
            "  - 默认 question=\"详细描述这张图片的内容\"——拿到基础描述\n"
            "  - 若你想看具体细节（图里的数字、代码截图、走势对比），question 改成精确的提问\n"
            "  - 一次调用 return ≤ 800 token，复杂图表可拆多次方问不同部分"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": "附件 ID，形如 feishu:img_v3_xxx 或 local:abc12345",
                },
                "question": {
                    "type": "string",
                    "description": "向视觉模型提的问题。空时默认「详细描述这张图片」。"
                                   "例：「图里出现的数字」「这张截图里的报错信息」「这张走势图的趋势如何」",
                    "default": "",
                },
            },
            "required": ["attachment_id"],
        },
    },
    handler=_handle_sense_image,
    check_fn=lambda: True,
    emoji="👁️",
)


registry.register(
    name="register_attachment",
    toolset="actions",
    schema={
        "name": "register_attachment",
        "description": (
            "把本地图片文件登记为附件（attachment_id），以便用 sense_image 查看。\n"
            "\n"
            "场景：\n"
            "  - 用 matplotlib / pyecharts 等生成了图，想用 vision 模型描述给上下文\n"
            "  - 用 terminal 下载了一张图（apps/<id>/workspace/...），想看内容\n"
            "  - 截图 or 本地图片想快速拿到 vision 描述\n"
            "\n"
            "注意：路径必须是**绝对路径**且**当前实例可读**。"
            "建议优先放在 apps/<id>/workspace/ 下（terminal / execute_code 默认 cwd）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "本地图片绝对路径（apps/<id>/workspace/...）",
                },
                "description": {
                    "type": "string",
                    "description": "可选：备注（如「7/14 候选池走势图」）。仅存档，不影响处理。",
                    "default": "",
                },
            },
            "required": ["path"],
        },
    },
    handler=_handle_register_attachment,
    check_fn=lambda: True,
    emoji="📎",
)
