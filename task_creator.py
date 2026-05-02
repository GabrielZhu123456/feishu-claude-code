"""task_creator.py - CLI → local inbox 标准任务创建入口（ADR-002）

单一写入源：CLI 创建 local inbox 任务时必须通过此模块。
其他代码不得直接绕过此模块写 command_inbox。

requires_confirmation 自动推导规则：
  L2/L3 → True（中高风险必须确认）
  L0/L1 → False（低风险免确认）
  调用方可以显式覆盖
"""
import hashlib
import json
import os
import random
import time
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("task_creator")

COMMAND_TABLE_ID = "tbl97NhYXZDJ52Vg"

# Reuse LightBitable from message_router
from message_router import LightBitable, _get_bitable, TASK_APP_TOKEN


RISK_REQUIRES_CONFIRMATION = {"L0": False, "L1": False, "L2": True, "L3": True}

VALID_INTENTS = ("run_task", "call_skill", "dispatch_agent",
                 "approve", "reject", "pause_task", "resume_task",
                 "escalate", "confirm", "query_status", "query_pending",
                 "add_comment", "help")
VALID_RISK_LEVELS = ("L0", "L1", "L2", "L3")


def create_local_task(
    action: str,
    parameters: dict = None,
    intent: str = "run_task",
    risk_level: str = "L0",
    requires_confirmation: Optional[bool] = None,
    session_id: str = "",
    source: str = "mobile_cli",
) -> dict:
    """按 ADR-002 标准 schema 创建 local inbox 任务。

    Args:
        action: 人类可读的动作描述
        parameters: 任务参数 dict
        intent: 任务意图，默认 run_task
        risk_level: 风险分级 L0-L3
        requires_confirmation: 是否需要确认，None 时自动推导
        session_id: CLI session ID
        source: 来源标识

    Returns:
        {"ok": bool, "task_id": str, "task": dict, "error": str|None}
    """
    # Validate
    if intent not in VALID_INTENTS:
        return {"ok": False, "task_id": "", "task": {},
                "error": f"invalid intent: {intent}"}
    if risk_level not in VALID_RISK_LEVELS:
        return {"ok": False, "task_id": "", "task": {},
                "error": f"invalid risk_level: {risk_level}"}

    # Auto-derive requires_confirmation
    if requires_confirmation is None:
        requires_confirmation = RISK_REQUIRES_CONFIRMATION.get(risk_level, False)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ts = int(datetime.now(timezone.utc).timestamp())
    task_id = f"LOC-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{random.randint(1000,9999)}"
    trace_id = f"trace_{ts}_{random.randint(1000, 9999)}"

    parameters = parameters or {}

    fields = {
        "command_id": task_id,
        "source_channel": "feishu_xhs",
        "user_id": source,
        "raw_text": action[:500],
        "intent": intent,
        "target_type": "task",
        "target_id": "",
        "params_json": json.dumps(parameters, ensure_ascii=False),
        "status": "new",
        "created_at": now_ms,
        "trace_id": trace_id,
        "dedupe_key": hashlib.sha256(
            f"{source}:{intent}:{action[:100]}:{ts}".encode()
        ).hexdigest()[:32],
        "needs_clarification": False,
        "human_summary": f"{intent}: {action[:60]}",
        # ADR-002 protocol fields
        "execution_scope": "local",
        "risk_level": risk_level,
        "session_id": session_id,
        "requires_confirmation": requires_confirmation,
    }

    bitable = _get_bitable()
    if not TASK_APP_TOKEN:
        return {"ok": False, "task_id": task_id, "task": fields,
                "error": "TASK_APP_TOKEN not configured"}

    rid = bitable.create_record(COMMAND_TABLE_ID, fields)
    if rid:
        logger.info("[task_creator] created task_id=%s intent=%s risk=%s rid=%s",
                     task_id, intent, risk_level, rid)
        return {"ok": True, "task_id": task_id, "task": fields, "error": None}
    else:
        logger.warning("[task_creator] write failed: task_id=%s", task_id)
        return {"ok": False, "task_id": task_id, "task": fields,
                "error": "write to command_inbox failed"}
