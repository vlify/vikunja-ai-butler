"""
Prompt composition, AI insight generation, and Markdown report assembly for evening digest.
"""

from typing import Optional
from butler.llm_runner import LLMRunner, LLMError, LLMTimeoutError


def build_digest_ai_insight(
    llm: Optional[LLMRunner],
    target_date: str,
    completed_section: str,
    aw_section: str,
) -> str:
    """
    Generate integrated AI insight paragraph linking completed tasks and screen time allocation.
    Gracefully degrades on timeout or failure.
    """
    if llm is None:
        return ""

    prompt = f"""你是一个客观、敏锐、富于洞察力的个人数字化效能管家。
请将用户今天的【今日已完成任务】与【ActivityWatch 屏幕时间分配】两部分数据深度结合起来，进行融为一体的晚间效能与精力复盘（约 150-200 字）。

核心要求：
1. 【深度交叉印证，严禁机械割裂】：
   - 绝不要把“完成了什么”和“屏幕时间几小时”拆成两半分别叙述！
   - 必须分析【时间投入】与【实际产出】的映射关系（时间投产比）：观察屏幕上的核心应用时长（如终端、开发环境、浏览器、文档等）是否真实印证并支撑了今日完成的硬核成果。
2. 【还原全天时间线与节奏】:
   - 结合【分时段时间线】逐时段还原一天的真实节奏:什么时候专注攻坚、什么时候缓冲娱乐、节奏是连续还是碎片化;
   - 【今日已完成任务】的完成时间是节点,时间线是过程——用过程解释节点,讲清楚全天精力如何流动;
   - 若发现时间投入与产出存在偏差（例如长时间处于配置/排查泥潭，或出现零碎应用切换），敏锐客观地指出真实的精力消耗点。
3. 【文风要求】：
   - 亲切诚恳、犀利敏锐，严禁官话、空话与说教。
   - 专注于今天真实的“付出与斩获”，不唠叨未竟事项。

今日汇总数据：
日期：{target_date}

{completed_section}

{aw_section}
"""
    try:
        raw = llm.run(prompt)
        text = raw.strip()
        return f"## 💡 AI 点评\n\n{text}"
    except LLMTimeoutError:
        return "## 💡 AI 点评\n\n⚠️ AI 点评生成失败 (退出状态码: 124)"
    except LLMError as e:
        return f"## 💡 AI 点评\n\n⚠️ AI 点评生成失败 (退出状态码: {e.exit_code})"
    except Exception as e:
        return f"## 💡 AI 点评\n\n⚠️ AI 点评生成失败: {e}"


def assemble_digest_report(
    report_title: str,
    completed_section: str,
    aw_section: str = "",
    ai_section: str = "",
) -> str:
    """
    Assemble complete evening digest report in Markdown format.
    晚间总结仅列已完成,不含待办。
    """
    sections = [f"# {report_title}", completed_section]
    if aw_section:
        sections.append(aw_section)
    if ai_section:
        sections.append(ai_section)

    return "\n\n".join(sections).strip() + "\n"
