"""message_router.py - 飞书消息 A/B/C 分类路由（Patch 1-3）"""
import os
import time
import requests
from datetime import datetime, timezone

# 任务表配置
TASK_APP_TOKEN = os.environ.get("AGENT_OS_TASK_APP_TOKEN", "")
TASK_TABLE_ID = os.environ.get("AGENT_OS_TASK_TABLE_ID", "")

# A/B/C 关键词
QUERY_KEYWORDS = ["查状态", "查进度", "查阻塞", "什么进度", "卡在哪", "当前任务", "任务状态", "运行状态"]
PROPOSAL_KEYWORDS = ["研究一下", "看看要不要做", "给个方案", "评估一下", "帮我分析", "可行性"]
TASK_KEYWORDS = ["改掉", "去跑一下", "今晚做", "帮我改", "修复", "实现", "上线"]


def classify_message(text: str) -> str:
    """A/B/C/unknown 分类，优先级 A>B>C"""
    for kw in QUERY_KEYWORDS:
        if kw in text:
            return "A"
    for kw in PROPOSAL_KEYWORDS:
        if kw in text:
            return "B"
    for kw in TASK_KEYWORDS:
        if kw in text:
            return "C"
    return "unknown"


class LightBitable:
    """轻量飞书 Bitable 客户端，仅 search + create"""
    BASE = "https://open.feishu.cn/open-apis"

    def __init__(self):
        self.app_id = os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        self._token = ""
        self._token_expires = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires:
            return self._token
        resp = requests.post(
            f"{self.BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        ).json()
        self._token = resp.get("tenant_access_token", "")
        self._token_expires = time.time() + resp.get("expire", 3600) - 300
        return self._token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def search_records(self, filter_dict=None, field_names=None, sort=None, page_size=20) -> list:
        if not TASK_APP_TOKEN:
            return []
        body = {}
        if filter_dict:
            body["filter"] = filter_dict
        if field_names:
            body["field_names"] = field_names
        if sort:
            body["sort"] = sort
        body["page_size"] = page_size
        try:
            resp = requests.post(
                f"{self.BASE}/bitable/v1/apps/{TASK_APP_TOKEN}/tables/{TASK_TABLE_ID}/records/search",
                headers=self._headers(),
                json=body,
                timeout=10,
            ).json()
            return resp.get("data", {}).get("items", []) if resp.get("code") == 0 else []
        except Exception:
            return []

    def create_record(self, fields: dict) -> str:
        if not TASK_APP_TOKEN:
            return ""
        try:
            resp = requests.post(
                f"{self.BASE}/bitable/v1/apps/{TASK_APP_TOKEN}/tables/{TASK_TABLE_ID}/records",
                headers=self._headers(),
                json={"fields": fields},
                timeout=10,
            ).json()
            return (
                resp.get("data", {}).get("record", {}).get("record_id", "")
                if resp.get("code") == 0
                else ""
            )
        except Exception:
            return ""


# 模块级单例
_bitable = None


def _get_bitable():
    global _bitable
    if _bitable is None:
        _bitable = LightBitable()
    return _bitable


def handle_query(text: str) -> str:
    """A 类：查询任务状态"""
    bitable = _get_bitable()
    if not TASK_APP_TOKEN:
        return "任务表未配置，请设置 AGENT_OS_TASK_APP_TOKEN / AGENT_OS_TASK_TABLE_ID"
    items = bitable.search_records(
        field_names=["status", "task_type", "creator_comment", "created_at"],
        page_size=50,
    )
    counts = {}
    for item in items:
        s = item.get("fields", {}).get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    # 最近 5 条
    recent = items[:5]
    lines = ["**任务状态总览**"]
    lines.append(" | ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    lines.append(f"\n**最近 {len(recent)} 条任务：**")
    for i, item in enumerate(recent, 1):
        f = item.get("fields", {})
        lines.append(
            f"{i}. [{f.get('status', '?')}] {f.get('task_type', '?')} - {str(f.get('creator_comment', ''))[:30]}"
        )
    return "\n".join(lines)


def handle_proposal(text: str, user_open_id: str) -> str:
    """B 类：创建 proposal"""
    bitable = _get_bitable()
    if not TASK_APP_TOKEN:
        return "任务表未配置"
    fields = {
        "project_id": "agent_os",
        "run_id": f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "task_type": "agent_os.proposal",
        "status": "pending",
        "skill_id": "coordinator_route",
        "approval_state": "pending_approval",
        "task_source": "feishu_message",
        "created_by": f"user:{user_open_id[:12]}",
        "coordinated_by": "agent:MAC-claude",
        "creator_comment": text[:500],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rid = bitable.create_record(fields)
    if rid:
        return f"已创建 proposal，等待审批。标题：{text[:50]}"
    return "创建 proposal 失败，请稍后重试"


def handle_task(text: str, user_open_id: str) -> str:
    """C 类：创建 task"""
    bitable = _get_bitable()
    if not TASK_APP_TOKEN:
        return "任务表未配置"
    fields = {
        "project_id": "agent_os",
        "run_id": f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}",
        "task_type": "agent_os.task",
        "status": "pending",
        "skill_id": "coordinator_route",
        "approval_state": "pending_approval",
        "task_source": "feishu_message",
        "created_by": f"user:{user_open_id[:12]}",
        "coordinated_by": "agent:MAC-claude",
        "creator_comment": text[:500],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    rid = bitable.create_record(fields)
    if rid:
        return f"已创建任务，等待审批后路由执行。标题：{text[:50]}"
    return "创建任务失败，请稍后重试"


def route_message(text: str, user_open_id: str):
    """主路由入口。返回回复文本或 None（走原 Claude CLI）"""
    category = classify_message(text)
    print(f"[route] classify={category} text={text[:60]}", flush=True)
    if category == "unknown":
        return None
    if category == "A":
        return handle_query(text)
    elif category == "B":
        return handle_proposal(text, user_open_id)
    else:  # C
        return handle_task(text, user_open_id)
