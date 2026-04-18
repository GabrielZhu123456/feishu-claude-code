"""message_router.py - 移动端传话筒模式：所有消息写入 command_inbox

角色定义（SPEC Mobile Messenger v1.1）：
  - xiaohongshu_comments 是输入代理（传话筒），不是执行者
  - 只写 command_inbox，不直接查表/建任务/回复执行结果
  - 统一回复"已收到，指令已提交处理"

使用方式:
  from message_router import route_message
  reply = route_message(text, user_open_id)
  # reply = "已收到，指令已提交处理。" 或 错误消息
"""
import hashlib
import os
import re
import time
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger("message_router")

# 飞书表配置
TASK_APP_TOKEN = os.environ.get("AGENT_OS_TASK_APP_TOKEN", "")
TASK_TABLE_ID = os.environ.get("AGENT_OS_TASK_TABLE_ID", "")
COMMAND_TABLE_ID = "tbl97NhYXZDJ52Vg"  # command_inbox

# ── Intent 关键词（覆盖 CommandWriter 全部 9 个 intent + other）──

INTENT_KEYWORDS = {
    "query_status": [
        "状态", "怎么样", "系统", "概况", "运行", "进度", "什么情况",
        "报了个", "出错", "跑得怎么样", "系统状况",
    ],
    "query_pending": [
        "pending", "待处理", "等什么", "有哪些", "pending的",
        "在跑什么", "有什么任务", "在做什么", "正在跑",
        "有什么项目", "项目在跑", "跑到哪",
    ],
    "approve": [
        "批准", "通过", "同意", "确认", "approve", "ok批准",
    ],
    "reject": [
        "拒绝", "驳回", "取消", "不要", "reject", "否决",
    ],
    "pause_task": [
        "暂停", "停一下", "别跑", "先别", "停掉", "pause",
    ],
    "resume_task": [
        "恢复", "继续", "跑起来", "resume", "接着跑",
    ],
    "add_comment": [
        "备注", "加个", "记一下", "comment", "标注",
    ],
    "escalate": [
        "紧急", "尽快", "马上", "优先", "加急", "escalate", "很急",
    ],
    "help": [
        "帮助", "你能做什么", "指令", "help", "命令", "你会什么",
        "能做什么", "功能",
    ],
}

# 疑问句模式（语义兜底用）
QUESTION_PATTERNS = [
    r"[吗呢吧啊？?]",           # 句末疑问词
    r"(什么|怎么|哪些|多少|几)",  # 疑问代词
    r"(有没有|是不是|能不能)",   # 正反问句
]

# 祈使句/行动模式（语义兜底用）
ACTION_PATTERNS = [
    r"(帮我|给我|给我|去|把|让)",  # 祈使词
    r"(研究|分析|看看|评估|调查)",  # 分析类
    r"(改|修|做|跑|上线|实现)",    # 行动类
]


def _score_intent(text: str) -> tuple:
    """关键词评分，返回 (intent, score)。"""
    text_lower = text.lower()
    best_intent, best_score = "other", 0
    for intent, keywords in INTENT_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > best_score:
            best_score = score
            best_intent = intent
    return best_intent, best_score


def _semantic_classify(text: str) -> str:
    """关键词未匹配时的语义兜底分类。

    规则：
    - 包含疑问模式 → query_status（用户想了解状况）
    - 包含行动模式 + 研究类词 → query_pending（用户想了解详情）
    - 包含行动模式 + 其他 → other（记录待处理）
    - 其余 → other
    """
    # 疑问句
    for pat in QUESTION_PATTERNS:
        if re.search(pat, text):
            return "query_status"

    # 行动句
    has_action = any(re.search(pat, text) for pat in ACTION_PATTERNS)
    research_words = ["研究", "分析", "看看", "评估", "调查", "了解"]
    if has_action and any(w in text for w in research_words):
        return "query_pending"

    return "other"


def classify_message(text: str) -> tuple:
    """分类消息，返回 (intent, target_type, target_id)。

    优先级：关键词匹配 > 语义兜底 > other
    """
    intent, score = _score_intent(text)
    if score == 0:
        intent = _semantic_classify(text)

    # target_type 简单推断
    target_type = "none"
    target_id = ""
    if intent in ("approve", "reject", "add_comment", "escalate",
                  "pause_task", "resume_task"):
        if any(kw in text.lower() for kw in ["proposal", "方案", "提案"]):
            target_type = "proposal"
        elif any(kw in text.lower() for kw in ["task", "任务"]):
            target_type = "task"

    return intent, target_type, target_id


# ── LightBitable（轻量飞书客户端）──


class LightBitable:
    """轻量飞书 Bitable 客户端，仅 search + create。"""
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

    def create_record(self, table_id: str, fields: dict) -> str:
        """创建记录到指定表。返回 record_id。"""
        if not TASK_APP_TOKEN:
            return ""
        try:
            resp = requests.post(
                f"{self.BASE}/bitable/v1/apps/{TASK_APP_TOKEN}/tables/{table_id}/records",
                headers=self._headers(),
                json={"fields": fields},
                timeout=10,
            ).json()
            return (
                resp.get("data", {}).get("record", {}).get("record_id", "")
                if resp.get("code") == 0
                else ""
            )
        except Exception as exc:
            logger.warning("[bitable] create error: %s", exc)
            return ""


# 模块级单例
_bitable = None


def _get_bitable():
    global _bitable
    if _bitable is None:
        _bitable = LightBitable()
    return _bitable


# ── command_inbox 写入 ──


def _write_command(text: str, user_id: str, intent: str,
                   target_type: str, target_id: str) -> bool:
    """写入 command_inbox 表。"""
    bitable = _get_bitable()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    import random
    command_id = f"cmd_{now_ms}_{random.randint(1000, 9999)}"
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dedupe_key = hashlib.sha256(
        f"{user_id}{intent}{target_id}{date_str}".encode()
    ).hexdigest()

    fields = {
        "command_id": command_id,
        "source_channel": "feishu_xhs",
        "user_id": user_id,
        "raw_text": text[:500],
        "intent": intent,
        "target_type": target_type,
        "target_id": target_id,
        "params_json": "{}",
        "status": "new",
        "created_at": now_ms,
        "trace_id": f"trace_{command_id}",
        "dedupe_key": dedupe_key,
        "needs_clarification": intent == "other",
        "human_summary": f"{intent}: {text[:60]}",
    }
    rid = bitable.create_record(COMMAND_TABLE_ID, fields)
    if rid:
        logger.info("[command_writer] wrote command_id=%s intent=%s rid=%s",
                     command_id, intent, rid)
    else:
        logger.warning("[command_writer] write failed: intent=%s", intent)
    return bool(rid)


# ── 主路由入口 ──


def route_message(text: str, user_open_id: str):
    """传话筒模式：所有消息写入 command_inbox。

    Returns:
      str: 回复文本（已提交 / 错误提示）
      None: 降级到 Claude CLI（仅 command_inbox 表不可用时）
    """
    if not TASK_APP_TOKEN:
        return None  # 表未配置，降级到 Claude CLI

    intent, target_type, target_id = classify_message(text)
    logger.info("[route] text=%s → intent=%s target=%s",
                text[:60], intent, target_type)

    ok = _write_command(text, user_open_id, intent, target_type, target_id)
    if ok:
        if intent == "other":
            return "已收到，指令已提交处理。如需更精确的操作，可以告诉我具体要做什么（如：查状态、批准、暂停等）。"
        return "已收到，指令已提交处理。"
    else:
        # 写入失败，降级到 Claude CLI
        logger.warning("[route] command write failed, falling back to Claude CLI")
        return None
