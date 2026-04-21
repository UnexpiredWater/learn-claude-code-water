#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
my_subagent.py - 简易子 Agent 启动器

用法:
    python agents/my_subagent.py "你的任务描述"
    
示例:
    python agents/my_subagent.py "帮我分析当前项目的目录结构"
    python agents/my_subagent.py "创建一个简单的 Python 计算器"
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

SYSTEM = f"""你是一个编码子 Agent，工作目录在 {WORKDIR}。
你的任务是完成用户指定的目标。你可以使用以下工具：
- bash: 运行 shell 命令
- read_file: 读取文件内容
- write_file: 写入文件内容（创建或覆盖文件）
- edit_file: 替换文件中的文本
- todo: 更新任务列表，跟踪多步骤任务的进度

使用 todo 工具来规划多步骤任务。开始任务前标记为 in_progress，完成后标记为 completed。
完成所有任务后，请提供一个简洁的总结。
"""


# -- TodoManager: 结构化状态管理 --
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


# -- 工具实现 --
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


TOOL_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "todo": lambda **kw: TODO.update(kw["items"]),
}

TOOLS = [
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
        "description": "更新任务列表。跟踪多步骤任务的进度。",
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


def run_agent(task: str) -> str:
    """运行子 Agent 完成任务"""
    messages = [{"role": "user", "content": f"任务：{task}"}]
    
    print(f"\n🤖 子 Agent 开始执行任务：{task}\n")
    
    for i in range(30):  # 最大迭代次数
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        
        messages.append({"role": "assistant", "content": response.content})
        
        # 如果没有工具调用，说明任务完成
        if response.stop_reason != "tool_use":
            break
            
        # 处理工具调用
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                if handler:
                    print(f"  🔧 使用工具：{block.name}")
                    output = handler(**block.input)
                    print(f"     结果：{str(output)[:100]}...")
                else:
                    output = f"未知工具：{block.name}"
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output)[:50000]
                })
        
        messages.append({"role": "user", "content": results})
    
    # 提取最终结果
    result_parts = []
    for block in response.content:
        if hasattr(block, "text") and block.text:
            result_parts.append(block.text)
    
    result = "\n".join(result_parts) if result_parts else "(无结果)"
    print(f"\n✅ 任务完成\n")
    return result


def main():
    if len(sys.argv) < 2:
        print("用法：python agents/my_subagent.py \"任务描述\"")
        print("\n示例:")
        print('  python agents/my_subagent.py "分析当前项目结构"')
        print('  python agents/my_subagent.py "创建一个简单的 Python 计算器"')
        sys.exit(1)
    
    task = " ".join(sys.argv[1:])
    result = run_agent(task)
    print("=" * 60)
    print("📋 执行结果:")
    print("=" * 60)
    print(result)


if __name__ == "__main__":
    main()