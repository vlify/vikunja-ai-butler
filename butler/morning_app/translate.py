"""
LLM batch translation for morning news and notification titles.
"""

import re
import sys
import json
from typing import List, Optional

from butler.morning_app.models import MorningItem
from butler.llm import LLMProvider


def translate_items_batch(items: List[MorningItem], llm: Optional[LLMProvider]) -> None:
    """
    Translate titles/descriptions to Chinese using a SINGLE batch LLM call.
    If items is empty, does NOT invoke LLM.
    If LLM fails, times out, or output is malformed, falls back to original text.
    """
    if not items:
        return

    if llm is None:
        for item in items:
            item.translated_title = item.title
        return

    lines_to_translate = []
    for idx, item in enumerate(items, 1):
        lines_to_translate.append(f"{idx}. {item.title}")

    prompt = (
        "你是一个专业的技术翻译助手。请将以下待办与通知英文标题准确翻译为简洁的中文（保持专业术语如 CI, MR, PR, Bug, Segfault 不变）。\n"
        "严格返回 JSON 数组格式，不要输出任何其他内容。\n"
        "格式示例: [{\"index\": 1, \"translation\": \"中文翻译\"}]\n\n"
        "待翻译列表:\n" + "\n".join(lines_to_translate)
    )

    try:
        raw_output = llm.run(prompt)
        if not raw_output or not raw_output.strip():
            for item in items:
                item.translated_title = item.title
            return

        m = re.search(r"\[\s*\{.*\}\s*\]", raw_output, re.DOTALL)
        if m:
            json_str = m.group(0)
            data = json.loads(json_str)
            trans_map = {}
            for entry in data:
                if isinstance(entry, dict) and "index" in entry and "translation" in entry:
                    trans_map[int(entry["index"])] = str(entry["translation"]).strip()

            for idx, item in enumerate(items, 1):
                if idx in trans_map and trans_map[idx]:
                    item.translated_title = trans_map[idx]
                else:
                    item.translated_title = item.title
        else:
            for item in items:
                item.translated_title = item.title
    except Exception as e:
        print(f"[WARN] LLM translation failed or timed out: {e}. Falling back to original titles.", file=sys.stderr)
        for item in items:
            item.translated_title = item.title
