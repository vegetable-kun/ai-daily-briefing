#!/home/ecs-assist-user/.hermes/scripts/.venv/bin/python3
"""Cron wrapper — imports and runs the real briefing generator."""
import sys
sys.path.insert(0, '/home/ecs-assist-user/.hermes/scripts/tavily_shim')
sys.path.insert(0, '/home/ecs-assist-user/.hermes/skills/crawl4ai-daily-briefing')
from briefing import main

if __name__ == '__main__':
    main()
