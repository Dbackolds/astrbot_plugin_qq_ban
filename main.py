import json
import os
from typing import Dict, Iterable, Set

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

try:  # pragma: no cover - 协议端可选依赖
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
except Exception:  # pragma: no cover
    AiocqhttpMessageEvent = None


@register(
    "astrbot_plugin_qq_ban",
    "Cascade",
    "QQ 退群黑名单插件，自动拦截黑名单用户再次入群",
    "0.1.0",
    "",
)
class QQBanPlugin(Star):
    """针对 QQ 群的退群黑名单管理插件。"""

    DATA_DIR = os.path.join("data", "plugins", "astrbot_plugin_qq_ban")

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig = config or AstrBotConfig({})
        self.enforce_whitelist: bool = self.config.get("enable_group_whitelist", True)
        self.group_whitelist: Set[str] = {
            str(gid).strip() for gid in self.config.get("group_whitelist", []) if gid
        }
        self.notice_enabled: bool = self.config.get("enable_blacklist_notice", True)
        self.auto_approve_enabled: bool = self.config.get("enable_auto_approve", False)
        self.reject_reason: str = (
            self.config.get("reject_reason")
            or "黑名单成员，拒绝加入。"
        )
        self.leave_notice_template: str = (
            self.config.get("leave_notice_template")
            or "成员 {member} 已退出群聊，并被加入黑名单。"
        )

        os.makedirs(self.DATA_DIR, exist_ok=True)

    # ----------------------------- 文件辅助方法 -----------------------------
    def _group_dir(self, group_id: str) -> str:
        return os.path.join(self.DATA_DIR, group_id)

    def _blacklist_file(self, group_id: str) -> str:
        return os.path.join(self._group_dir(group_id), "blacklist.json")

    def _load_blacklist(self, group_id: str) -> Set[str]:
        path = self._blacklist_file(group_id)
        if not os.path.exists(path):
            return set()
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
                return {str(uid) for uid in data}
        except Exception as exc:  # pragma: no cover - 容错
            logger.error(f"读取黑名单失败: {exc}")
            return set()

    def _save_blacklist(self, group_id: str, members: Iterable[str]) -> None:
        os.makedirs(self._group_dir(group_id), exist_ok=True)
        path = self._blacklist_file(group_id)
        try:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(sorted({str(uid) for uid in members}), file, ensure_ascii=False, indent=2)
        except Exception as exc:  # pragma: no cover
            logger.error(f"写入黑名单失败: {exc}")

    def _add_to_blacklist(self, group_id: str, user_id: str) -> bool:
        members = self._load_blacklist(group_id)
        if user_id in members:
            return False
        members.add(user_id)
        self._save_blacklist(group_id, members)
        logger.info(f"[QQBan] {user_id} 已加入群 {group_id} 黑名单")
        return True

    def _in_blacklist(self, group_id: str, user_id: str) -> bool:
        return user_id in self._load_blacklist(group_id)

    def _group_allowed(self, group_id: str) -> bool:
        if not group_id:
            return False
        if not self.enforce_whitelist:
            return True
        return group_id in self.group_whitelist

    # ---------------------------- 事件分发逻辑 -----------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_group_events(self, event: AstrMessageEvent):
        """监听 OneBot 事件 Raw Payload，捕获退群通知与加群申请。"""

        raw = self._extract_raw_payload(event)
        if not raw:
            return

        post_type = raw.get("post_type")
        if post_type == "notice" and raw.get("notice_type") == "group_decrease":
            async for result in self._handle_group_decrease(event, raw):
                yield result
        elif post_type == "request" and raw.get("request_type") == "group":
            async for result in self._handle_group_request(event, raw):
                yield result

    # ---------------------------- Notice 处理 ------------------------------
    async def _handle_group_decrease(
        self, event: AstrMessageEvent, raw: Dict
    ):
        group_id = str(raw.get("group_id") or getattr(event.message_obj, "group_id", ""))
        user_id = str(raw.get("user_id") or "")
        if not group_id or not user_id:
            return
        if not self._group_allowed(group_id):
            return

        added = self._add_to_blacklist(group_id, user_id)
        if added and self.notice_enabled:
            message = self._render_leave_notice(group_id, user_id)
            yield event.plain_result(message)

    # ---------------------------- Request 处理 -----------------------------
    async def _handle_group_request(
        self, event: AstrMessageEvent, raw: Dict
    ):
        group_id = str(raw.get("group_id") or getattr(event.message_obj, "group_id", ""))
        user_id = str(raw.get("user_id") or "")
        flag = raw.get("flag")
        sub_type = raw.get("sub_type", "add")  # add / invite
        if not (group_id and user_id and flag):
            return
        if not self._group_allowed(group_id):
            return

        if self._in_blacklist(group_id, user_id):
            await self._process_group_request(event, flag, sub_type, approve=False)
            if self.notice_enabled:
                yield event.plain_result(
                    f"检测到黑名单成员 {self._format_member(user_id)} 申请入群，已自动拒绝。"
                )
            return

        if self.auto_approve_enabled:
            success = await self._process_group_request(event, flag, sub_type, approve=True)
            if success and self.notice_enabled:
                yield event.plain_result(
                    f"成员 {self._format_member(user_id)} 的入群申请已自动同意。"
                )

    async def _process_group_request(
        self,
        event: AstrMessageEvent,
        flag: str,
        sub_type: str,
        approve: bool,
    ) -> bool:
        """调用协议端 API 处理加群申请，仅支持 aiocqhttp。"""

        if event.get_platform_name() != "aiocqhttp" or AiocqhttpMessageEvent is None:
            logger.warning("[QQBan] 当前平台不支持自动处理加群请求。")
            return False
        if not isinstance(event, AiocqhttpMessageEvent):
            logger.warning("[QQBan] 事件对象不是 AiocqhttpMessageEvent，无法调用 API。")
            return False

        client = event.bot
        payload = {
            "flag": flag,
            "sub_type": sub_type,
            "approve": approve,
        }
        if not approve:
            payload["reason"] = self.reject_reason

        try:
            await client.api.call_action("set_group_add_request", **payload)
            action = "通过" if approve else "拒绝"
            logger.info(f"[QQBan] 已自动{action} group_add_request")
            return True
        except Exception as exc:  # pragma: no cover
            logger.error(f"[QQBan] 处理加群请求失败: {exc}")
            return False

    # ---------------------------- 工具方法 --------------------------------
    @staticmethod
    def _extract_raw_payload(event: AstrMessageEvent) -> Dict | None:
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if isinstance(raw, dict):
            return raw
        return None

    @staticmethod
    def _format_member(user_id: str) -> str:
        return f"[CQ:at,qq={user_id}]" if user_id.isdigit() else user_id

    def _render_leave_notice(self, group_id: str, user_id: str) -> str:
        data = {
            "member": self._format_member(user_id),
            "user_id": user_id,
            "group_id": group_id,
        }
        try:
            return self.leave_notice_template.format(**data)
        except Exception as exc:  # pragma: no cover
            logger.error(f"[QQBan] leave_notice_template 渲染失败: {exc}")
            return f"成员 {data['member']} 已退出群聊，并被加入黑名单。"

    async def terminate(self):  # pragma: no cover
        logger.info("[QQBan] 插件已卸载。")
