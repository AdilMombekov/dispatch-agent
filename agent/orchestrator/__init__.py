"""Dispatch task orchestrator.

A local task queue (SQLite) + worker (Dispatcher) that routes queued tasks to
executors (Haiku API / claude CLI / computer-use — wired in later steps) and
reports progress to Telegram.

Built in steps (see TODO.md → P3):
  9.  queue + dispatcher + Telegram commands (this module's foundation)
  10. HaikuClient (qa)
  11. ClaudeCliClient (code)
  12. computer-use (click_gui)
  13. SonnetReviewer + budget
  14. APScheduler (scheduled)
"""
