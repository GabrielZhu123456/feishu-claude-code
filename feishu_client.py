"""
飞书 API 异步封装。
流式方案：CardKit 流式卡片（打字机效果）+ PATCH 兜底。
"""

import asyncio
import json
import os
import tempfile
import time
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1.model import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


def _card_json(content: str, loading: bool = False) -> str:
    """
    生成卡片 JSON 字符串（Card JSON 2.0）

    飞书卡片 markdown 元素有长度限制（约 3000 字符），
    超过限制时自动分段为多个 markdown 元素。
    """
    elements = []
    if loading:
        elements.append({"tag": "markdown", "content": "⏳ 思考中..."})
    else:
        # 飞书 markdown 元素长度限制约 3000 字符，保守使用 2800
        MAX_CHUNK_SIZE = 2800

        if len(content) <= MAX_CHUNK_SIZE:
            # 内容不长，直接发送
            elements.append({"tag": "markdown", "content": content})
        else:
            # 内容过长，分段发送
            # 尝试按段落分割，避免在句子中间截断
            chunks = []
            current_chunk = ""

            # 按换行符分割
            lines = content.split('\n')

            for line in lines:
                # 如果单行就超过限制，强制截断
                if len(line) > MAX_CHUNK_SIZE:
                    # 先保存当前块
                    if current_chunk:
                        chunks.append(current_chunk)
                        current_chunk = ""

                    # 强制分割长行
                    for i in range(0, len(line), MAX_CHUNK_SIZE):
                        chunks.append(line[i:i + MAX_CHUNK_SIZE])
                    continue

                # 检查加上这行是否会超过限制
                if len(current_chunk) + len(line) + 1 > MAX_CHUNK_SIZE:
                    # 超过限制，保存当前块，开始新块
                    if current_chunk:
                        chunks.append(current_chunk)
                    current_chunk = line
                else:
                    # 未超过限制，追加到当前块
                    if current_chunk:
                        current_chunk += '\n' + line
                    else:
                        current_chunk = line

            # 保存最后一块
            if current_chunk:
                chunks.append(current_chunk)

            # 为每个块创建 markdown 元素
            for i, chunk in enumerate(chunks):
                # 第一块不加前缀，后续块加分段标记
                if i > 0:
                    chunk = f"**（续 {i}）**\n\n{chunk}"
                elements.append({"tag": "markdown", "content": chunk})

    return json.dumps({
        "schema": "2.0",
        "body": {"elements": elements},
    }, ensure_ascii=False)


class FeishuClient:
    def __init__(self, client: lark.Client, app_id: str = "", app_secret: str = ""):
        self.client = client
        self._app_id = app_id
        self._app_secret = app_secret

    async def _retry_with_backoff(self, coro_func, max_retries: int = 3, initial_delay: float = 0.5):
        """
        执行异步操作，失败时指数退避重试。

        Args:
            coro_func: 返回 coroutine 的可调用对象
            max_retries: 最多重试次数（不包括首次尝试）
            initial_delay: 初始延迟秒数

        Returns:
            操作结果

        Raises:
            最后一次尝试的异常
        """
        delay = initial_delay
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                return await coro_func()
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    print(f"[retry] 第 {attempt + 1} 次失败，{delay:.1f}s 后重试: {e}", flush=True)
                    await asyncio.sleep(delay)
                    delay *= 2  # 指数退避
                else:
                    print(f"[retry] 已达最大重试次数 {max_retries + 1}，放弃", flush=True)

        raise last_error

    # ── 发送消息 ──────────────────────────────────────────────

    async def send_card_to_user(self, open_id: str, content: str = "", loading: bool = True) -> str:
        """向用户发送卡片消息，返回 message_id（带重试）"""
        async def _send():
            req = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(open_id)
                    .msg_type("interactive")
                    .content(_card_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.acreate(req)
            if not resp.success():
                raise RuntimeError(f"发送卡片消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        return await self._retry_with_backoff(_send, max_retries=3)

    async def reply_card(self, message_id: str, content: str = "", loading: bool = True) -> str:
        """回复用户消息（卡片形式），触发通知。返回回复消息的 message_id（带重试）"""
        async def _reply():
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("interactive")
                    .content(_card_json(content, loading=loading))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                raise RuntimeError(f"回复卡片消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        return await self._retry_with_backoff(_reply, max_retries=3)

    async def update_card(self, message_id: str, content: str):
        """用 patch 更新已发送的卡片内容（带重试）"""
        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(_card_json(content, loading=False))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=3)

    async def download_image(self, message_id: str, image_key: str) -> str:
        """下载飞书图片到临时文件，返回本地路径（不阻塞事件循环）"""
        return await asyncio.to_thread(
            self._download_image_sync, message_id, image_key
        )

    def _download_image_sync(self, message_id: str, image_key: str) -> str:
        """同步下载逻辑，在线程池中执行"""
        import ssl
        import urllib.request
        import uuid

        ctx = ssl.create_default_context()

        token_body = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
        token_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=token_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, context=ctx, timeout=10) as r:
            token = json.loads(r.read())["tenant_access_token"]

        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image"
        img_req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        tmp_path = os.path.join(tempfile.gettempdir(), f"feishu-img-{uuid.uuid4().hex[:8]}.jpg")
        with urllib.request.urlopen(img_req, context=ctx, timeout=15) as r:
            ct = r.headers.get("Content-Type", "")
            if "png" in ct:
                tmp_path = tmp_path.replace(".jpg", ".png")
            elif "gif" in ct:
                tmp_path = tmp_path.replace(".jpg", ".gif")
            with open(tmp_path, "wb") as f:
                f.write(r.read())

        return tmp_path

    async def update_card_with_buttons(self, message_id: str, content: str, buttons: list[dict],
                                      flow: bool = False):
        """更新卡片内容并附加操作按钮。flow=True 时横排自动换行，False 时竖排。"""
        base = json.loads(_card_json(content))
        btn_elements = []
        for i, btn in enumerate(buttons):
            btn_elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn["text"]},
                "type": "default",
                "size": "small",
                "name": f"btn_{i}",
                "value": btn["value"],
                "behaviors": [{"type": "callback", "value": btn["value"]}],
            })
        if flow and btn_elements:
            # 横排: column_set + flex_mode flow
            columns = [{"tag": "column", "width": "auto", "elements": [b]} for b in btn_elements]
            base["body"]["elements"].append({"tag": "column_set", "flex_mode": "flow", "columns": columns})
        else:
            # 竖排: 每个按钮独占一行
            base["body"]["elements"].extend(btn_elements)
        card_content = json.dumps(base, ensure_ascii=False)

        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(card_content)
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=3)

    async def update_card_elements(self, message_id: str, elements: list[dict]):
        """用自定义 elements 列表更新卡片（支持 markdown + button 混排）"""
        card_content = json.dumps({
            "schema": "2.0",
            "body": {"elements": elements},
        }, ensure_ascii=False)

        async def _update():
            req = (
                PatchMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    PatchMessageRequestBody.builder()
                    .content(card_content)
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.apatch(req)
            if not resp.success():
                raise RuntimeError(f"patch 卡片失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=3)

    # ── CardKit 流式卡片 ──────────────────────────────────────

    async def create_streaming_card(self, open_id: str = None, message_id: str = None,
                                     element_id: str = "md_stream") -> tuple[str, str]:
        """
        创建 CardKit 流式卡片并发送。返回 (card_id, message_id)。
        open_id 模式：主动发送给用户。message_id 模式：回复消息。
        """
        from lark_oapi.api.cardkit.v1.model import (
            CreateCardRequest, CreateCardRequestBody,
        )

        card_json = json.dumps({
            "schema": "2.0",
            "config": {
                "streaming_mode": True,
                "streaming_config": {
                    "print_frequency_ms": {"default": 50},
                    "print_step": {"default": 2},
                    "print_strategy": "fast",
                },
            },
            "body": {"elements": [
                {"tag": "markdown", "element_id": element_id, "content": "⏳ 思考中..."},
            ]},
        }, ensure_ascii=False)

        # Step 1: 创建卡片实体
        async def _create():
            req = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(card_json)
                    .build()
                )
                .build()
            )
            resp = await self.client.cardkit.v1.card.acreate(req)
            if not resp.success():
                raise RuntimeError(f"创建流式卡片失败: {resp.code} {resp.msg}")
            return resp.data.card_id

        card_id = await self._retry_with_backoff(_create, max_retries=2)

        # Step 2: 通过消息 API 发送卡片实体
        card_ref = json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)

        if message_id:
            # 回复模式
            msg_id = await self._retry_with_backoff(lambda: self._reply_card_entity(message_id, card_ref), max_retries=2)
        else:
            # 主动发送模式
            msg_id = await self._retry_with_backoff(lambda: self._send_card_entity(open_id, card_ref), max_retries=2)

        return card_id, msg_id

    async def _send_card_entity(self, open_id: str, card_ref: str) -> str:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("interactive")
                .content(card_ref)
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"发送卡片实体失败: {resp.code} {resp.msg}")
        return resp.data.message_id

    async def _reply_card_entity(self, message_id: str, card_ref: str) -> str:
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(card_ref)
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.areply(req)
        if not resp.success():
            raise RuntimeError(f"回复卡片实体失败: {resp.code} {resp.msg}")
        return resp.data.message_id

    async def stream_update_text(self, card_id: str, element_id: str, content: str, sequence: int):
        """流式更新卡片文本（传全量文本 + 递增 sequence，客户端自动打字机效果）"""
        from lark_oapi.api.cardkit.v1.model import (
            ContentCardElementRequest, ContentCardElementRequestBody,
        )

        async def _update():
            req = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(element_id)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            resp = await self.client.cardkit.v1.card_element.acontent(req)
            if not resp.success():
                raise RuntimeError(f"流式更新失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_update, max_retries=1)

    async def finish_streaming(self, card_id: str, sequence: int = 999):
        """关闭流式模式，恢复按钮交互和转发"""
        from lark_oapi.api.cardkit.v1.model import (
            SettingsCardRequest, SettingsCardRequestBody,
        )

        settings_json = json.dumps({"streaming_mode": False})

        async def _finish():
            req = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_json)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            resp = await self.client.cardkit.v1.card.asettings(req)
            if not resp.success():
                raise RuntimeError(f"关闭流式模式失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_finish, max_retries=2)

    async def streaming_add_buttons(self, card_id: str, buttons: list[dict], flow: bool = False):
        """流式结束后追加按钮元素"""
        from lark_oapi.api.cardkit.v1.model import (
            CreateCardElementRequest, CreateCardElementRequestBody,
        )

        btn_elements = []
        for i, btn in enumerate(buttons):
            btn_elements.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn["text"]},
                "type": "default",
                "size": "small",
                "name": f"btn_{i}",
                "value": btn["value"],
                "behaviors": [{"type": "callback", "value": btn["value"]}],
            })

        if flow and btn_elements:
            element = {"tag": "column_set", "flex_mode": "flow",
                       "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in btn_elements]}
        else:
            element = btn_elements[0] if len(btn_elements) == 1 else {"tag": "column_set", "flex_mode": "flow",
                       "columns": [{"tag": "column", "width": "auto", "elements": [b]} for b in btn_elements]}
            # 多按钮时用 column_set flow

        async def _add():
            req = (
                CreateCardElementRequest.builder()
                .card_id(card_id)
                .request_body(
                    CreateCardElementRequestBody.builder()
                    .element(json.dumps(element, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            resp = await self.client.cardkit.v1.card_element.acreate(req)
            if not resp.success():
                raise RuntimeError(f"添加按钮失败: {resp.code} {resp.msg}")

        await self._retry_with_backoff(_add, max_retries=2)

    async def reply_text(self, message_id: str, text: str) -> str:
        """回复纯文本消息（触发通知）"""
        async def _reply():
            req = (
                ReplyMessageRequest.builder()
                .message_id(message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(json.dumps({"text": text}))
                    .build()
                )
                .build()
            )
            resp = await self.client.im.v1.message.areply(req)
            if not resp.success():
                raise RuntimeError(f"回复文本消息失败: {resp.code} {resp.msg}")
            return resp.data.message_id

        return await self._retry_with_backoff(_reply, max_retries=2)

    async def send_text_to_user(self, open_id: str, text: str) -> str:
        """发送纯文本消息"""
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = await self.client.im.v1.message.acreate(req)
        if not resp.success():
            raise RuntimeError(f"发送文本消息失败: {resp.code} {resp.msg}")
        return resp.data.message_id
