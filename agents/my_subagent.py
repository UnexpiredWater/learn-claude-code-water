#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
my_subagent.py - Subagent 实现（父Agent + 子Agent 上下文隔离）

参考 s04_subagent.py 文档实现:
- 父 Agent 拥有 task 工具，可以派生子 Agent
- 子 Agent 拥有独立的 messages[]，不污染父 Agent 上下文
- 子 Agent 完成后只返回摘要文本给父 Agent

用法:
    python agents/my_subagent.py
    
    交互模式下输入任务，父 Agent 会自动决定是否委派子任务。

架构:
    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: task       | ----------> | while tool_use:  |
    |   prompt="..."   |             |   call tools     |
    |                  |  summary    |   append results |
    |   result = "..." | <---------- | return last text |
    +------------------+             +------------------+

    Parent context stays clean. Subagent context is discarded.
"""

import os
import sys
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

WORKDIR = Path.cwd()

# 使用 ANTHROPIC_AUTH_TOKEN 进行认证
auth_token = os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")
base_url = os.getenv("ANTHROPIC_BASE_URL")

if base_url:
    client = Anthropic(base_url=base_url, auth_token=auth_token)
else:
    client = Anthropic(api_key=auth_token)

MODEL = os.environ.get("MODEL_ID", "claude-sonnet-4-20250514")

# 系统提示词
SYSTEM = f"""你是一个编码 Agent，工作目录在 {WORKDIR}。
你可以直接使用工具完成简单任务，也可以使用 task 工具将复杂任务委派给子 Agent。
子 Agent 拥有独立的上下文，完成后只返回摘要。这样可以保持你的上下文清晰。

使用 task 工具的场景：
- 需要读取多个文件来获取信息
- 需要执行多步骤的探索性任务
- 任务结果可以用简短摘要表达
"""

SUBAGENT_SYSTEM = f"""你是一个编码子 Agent，工作目录在 {WORKDIR}。
完成给定的任务，然后用简洁的文字总结你的发现。
你可以使用 bash、read_file、write_file、edit_file 和 todo 工具。
"""


# ============================================================
# TodoManager: 结构化状态管理
# ============================================================

class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        """更新 todo 列表"""
        if len(items) > 20:
            raise ValueError("最多允许 20 个 todo 项")
        validated = []
        in_progress_count = 0
        for i, item in enumerate(items):
            text = str(item.get("text", "")).strip()
            status = str(item.get("status", "pending")).lower()
            item_id = str(item.get("id", str(i + 1)))
            if not text:
                raise ValueError(f"项目 {item_id}: 需要文本内容")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"项目 {item_id}: 无效的状态 '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"id": item_id, "text": text, "status": status})
        if in_progress_count > 1:
            raise ValueError("同时只能有一个任务处于 in_progress 状态")
        self.items = validated
        return self.render()

    def render(self) -> str:
        """渲染 todo 列表"""
        if not self.items:
            return "没有 todo 项。"
        lines = []
        for item in self.items:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} #{item['id']}: {item['text']}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n(已完成 {done}/{len(self.items)} 项)")
        return "\n".join(lines)


TODO = TodoManager()


# ============================================================
# 工具实现（父 Agent 和子 Agent 共享）
# ============================================================

def safe_path(p: str) -> Path:
    """确保路径在 workspace 内"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"路径超出工作区范围：{p}")
    return path


def run_bash(command: str) -> str:
    """运行 shell 命令（跨平台兼容）"""
    # 危险命令检测
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "错误：危险命令已被阻止"

    # Windows 兼容性：将常见 Unix 命令转换为 Windows 命令
    cmd = command
    if os.name == 'nt':  # Windows
        # 替换常见 Unix 命令为 Windows 等效命令（包括命令中间的情况）
        replacements = [
            ("ls -la", "dir"),
            ("ls -", "dir -"),
            ("ls ", "dir "),
            ("ls", "dir"),
            ("cat ", "type "),
            (" | grep ", " | findstr "),
            ("grep ", "findstr "),
            (" | head ", " | powershell -command Get-Content -Head "),
            ("head ", "powershell -command Get-Content -Head "),
            (" | tail ", " | powershell -command Get-Content -Tail "),
            ("tail ", "powershell -command Get-Content -Tail "),
            ("pwd", "cd"),
            ("rm ", "del "),
            ("mkdir ", "md "),
            ("cp ", "copy "),
            ("mv ", "move "),
            ("find ", "dir "),
            (" | wc -l", " | find /c /v "),
        ]
        for unix_cmd, win_cmd in replacements:
            cmd = cmd.replace(unix_cmd, win_cmd)

    try:
        r = subprocess.run(
            cmd, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：超时 (120 秒)"
    except (FileNotFoundError, OSError) as e:
        return f"错误：{e}"


def run_read(path: str, limit: int = None) -> str:
    """读取文件内容"""
    try:
        lines = safe_path(path).read_text(encoding='utf-8').splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... (还有 {len(lines) - limit} 行)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"错误：{e}"


def run_write(path: str, content: str) -> str:
    """写入文件内容"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding='utf-8')
        return f"已写入 {len(content)} 字节到 {path}"
    except Exception as e:
        return f"错误：{e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """编辑文件内容"""
    try:
        fp = safe_path(path)
        content = fp.read_text(encoding='utf-8')
        if old_text not in content:
            return f"错误：在 {path} 中未找到指定文本"
        fp.write_text(content.replace(old_text, new_text, 1), encoding='utf-8')
        return f"已编辑 {path}"
    except Exception as e:
        return f"错误：{e}"


# 工具处理器映射
TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo": lambda **kw: TODO.update(kw["items"]),
}


# ============================================================
# 工具定义
# ============================================================

# 子 Agent 拥有的基础工具（不包含 task，禁止递归派生）
CHILD_TOOLS = [
    {
        "name": "bash",
        "description": "运行 shell 命令",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "读取文件内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "limit": {"type": "integer", "description": "最大行数"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "写入文件内容",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "edit_file",
        "description": "替换文件中的文本",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "old_text": {"type": "string", "description": "要替换的原文本"},
                "new_text": {"type": "string", "description": "新文本"}
            },
            "required": ["path", "old_text", "new_text"]
        }
    },
    {
        "name": "todo",
        "description": "更新任务列表，跟踪多步骤任务的进度",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "任务 ID"},
                            "text": {"type": "string", "description": "任务描述"},
                            "status": {
                                "type": "string",
                                "description": "任务状态",
                                "enum": ["pending", "in_progress", "completed"]
                            }
                        },
                        "required": ["id", "text", "status"]
                    },
                    "description": "任务列表"
                }
            },
            "required": ["items"]
        }
    },
]

# 父 Agent 的工具 = 基础工具 + task 工具（仅父端拥有）
PARENT_TOOLS = CHILD_TOOLS + [
    {
        "name": "task",
        "description": "派生一个子 Agent，它拥有独立的上下文（messages=[]）。"
                       "子 Agent 共享文件系统但不共享对话历史。"
                       "适合委派探索性任务、多文件读取、复杂操作等。"
                       "只有最终摘要会返回给你。",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "给子 Agent 的任务描述"},
                "description": {"type": "string", "description": "简短的任务描述（用于日志）"}
            },
            "required": ["prompt"]
        }
    },
]


# ============================================================
# Subagent: 独立上下文，执行完毕后只返回摘要
# ============================================================

def run_subagent(prompt: str) -> str:
    """
    启动一个子 Agent：
    - 全新的 messages=[]（上下文隔离）
    - 只拥有基础工具（无 task，禁止递归派生）
    - 执行完毕后只返回最终文本摘要
    - 子 Agent 的完整消息历史被丢弃
    """
    sub_messages = [{"role": "user", "content": prompt}]  # 全新上下文

    response = None
    for _ in range(30):  # 安全上限
        response = client.messages.create(
            model=MODEL,
            system=SUBAGENT_SYSTEM,
            messages=sub_messages,
            tools=CHILD_TOOLS,
            max_tokens=8000,
        )
        sub_messages.append({"role": "assistant", "content": response.content})

        # 如果没有工具调用，任务完成
        if response.stop_reason != "tool_use":
            break

        # 处理工具调用
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                if handler:
                    output = handler(**block.input)
                else:
                    output = f"未知工具：{block.name}"
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)[:50000]
                })
        sub_messages.append({"role": "user", "content": results})

    # 只返回最终文本 -- 子 Agent 的完整上下文在此丢弃
    if response is None:
        return "(子 Agent 未执行)"
    return "".join(
        b.text for b in response.content if hasattr(b, "text")
    ) or "(无摘要)"


# ============================================================
# 父 Agent 循环
# ============================================================

def agent_loop(messages: list):
    """
    父 Agent 的主循环：
    - 调用 LLM
    - 处理工具调用（包括 task 工具，会派生子 Agent）
    - 直到 LLM 不再请求工具为止
    """
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=PARENT_TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # 如果没有工具调用，结束循环
        if response.stop_reason != "tool_use":
            return

        # 处理工具调用
        results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "task":
                    # 派生子 Agent（上下文隔离的核心）
                    desc = block.input.get("description", "子任务")
                    prompt = block.input.get("prompt", "")
                    print(f"\n  🚀 派生子任务 ({desc}): {prompt[:80]}")
                    output = run_subagent(prompt)
                    print(f"  📋 子任务返回: {str(output)[:200]}")
                else:
                    # 普通工具调用
                    handler = TOOL_HANDLERS.get(block.name)
                    if handler:
                        output = handler(**block.input)
                    else:
                        output = f"未知工具：{block.name}"
                    print(f"  🔧 {block.name}: {str(output)[:150]}")

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)
                })
        messages.append({"role": "user", "content": results})


# ============================================================
# 入口：支持交互模式和命令行模式
# ============================================================

if __name__ == "__main__":
    # 命令行模式：python agents/my_subagent.py "任务描述"
    if len(sys.argv) >= 2:
        task = " ".join(sys.argv[1:])
        print(f"\n🤖 父 Agent 开始处理任务：{task}\n")
        history = [{"role": "user", "content": task}]
        agent_loop(history)
        # 打印最终回复
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(f"\n{block.text}")
        print()
        sys.exit(0)

    # 交互模式
    print("=" * 60)
    print("🤖 Subagent 模式 - 父 Agent 可委派子任务")
    print("   子 Agent 拥有独立上下文，完成后只返回摘要")
    print("   父 Agent 上下文保持清洁")
    print("=" * 60)
    print("输入任务 (q/exit 退出):\n")

    history = []
    while True:
        try:
            query = input("\033[36msubagent >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见！")
            break
        if query.strip().lower() in ("q", "exit", ""):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)

        # 打印最终回复
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(f"\n{block.text}")
        print()
