"""SCM Copilot 前端页面包（Gradio 三页：对话 / 审批 / 日报）。

每个模块提供 `build(base_url, api_key)` 工厂函数：在 `gr.Blocks` 上下文内
创建页面组件并绑定事件，返回页面可测组件（dict）。app.py 负责组装 Tabs。
"""
