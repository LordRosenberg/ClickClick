"""Host-neutral presentation hints; the assistant owns its browser and chat."""

HOST_PRESENTATION_GUIDANCE = """Assistant presentation:
After successful task delegation, automatically open the returned preview_url task screen preview using your available in-app browser capability when preview_support.status is supported and the link exists; otherwise open console_url task details. assistant_ui.pages selects the target URL and retains console_url for details. A queued task may wait for its first frame; keep its preview page without opening a second details tab. Reuse an existing page for the same task_id; do not open duplicate tabs on polling or request-key retries. Apply the same rule to a newly created linked follow-up using that child's URLs. If you have no browser-opening capability or opening fails, share clickable preview/details links and continue tracking; browser failure does not change phone task execution. Do not assume a particular assistant product, tool name, sidebar position or fixed emulator viewer port, or launch a system browser from the ClickClick service. Do not start extra capture/control sessions to open a preview. Only say a page opened after your browser tool confirms it.
After installing ClickClick or verifying the first MCP connection, briefly introduce ClickClick and basic usage in the user's language in the conversation, using get_status.assistant_onboarding. Explain actual readiness and unfinished setup separately. State the current HITL switch value and where to configure it; changing it affects new tasks only. Do not repeat the introduction on ordinary status polls, and do not start a phone task or billable model request just for onboarding."""


def task_page_presentation(tasks):
    pages = []
    for task in tasks:
        preview = task.get("preview_url")
        supported = (task.get("preview_support") or {}).get("status") == "supported"
        pages.append({"task_id": task["task_id"],
                      "url": preview if supported and preview else task["console_url"],
                      "console_url": task["console_url"]})
    return {
        "action": "open_task_pages", "preferred_browser": "in_app",
        "reuse_by": "task_id", "on_unavailable": "share_links",
        "browser_failure_affects_task": False,
        "pages": pages,
    }


def assistant_onboarding(*, hitl_enabled, setup_url):
    return {
        "when": "after_install_or_first_connection_verification",
        "repeat_on_status_poll": False, "language": "use_the_users_language",
        "summary": "ClickClick 是运行在你电脑上的手机任务助手，可操作已连接的 Android 应用。它使用自己配置的模型和授权独立执行手机任务。",
        "quick_start": [
            "在 Console 配置模型与授权，连接 Android 手机或模拟器，并检查初始化状态。",
            "用自然语言说明目标、相关信息和限制；助手派发后在内置浏览器优先打开任务投屏预览页，详情页查看进度；不支持投屏时打开详情页。",
            "可以查询、暂停或取消任务；人工暂停可恢复，具备恢复资格的预算检查点可明确追加预算后续跑。",
        ],
        "hitl": {"enabled": hitl_enabled, "settings_url": setup_url,
                 "location": "Console 设置 → 运行与学习 → HITL，修改后保存设置",
                 "switch_label": "必要时向我提问或确认（HITL）",
                 "purpose": "开启后可中途调整正在执行的任务，并让任务在需要澄清或选择时提问。",
                 "applies_to": "仅新任务；已有任务保持创建时的开关状态",
                 "default_policy": "助手引导首次安装默认开启，其他安装默认关闭；已有保存的选择优先",
                 "connection_changes_setting": False},
        "example": "打开时钟，查看秒表当前状态，不修改闹钟或设置。",
        "readiness": "另行说明 get_status 中的模型配置、设备初始化和待完成步骤；配置完成不代表凭据已经验证。",
        "test_task_required": False,
    }
