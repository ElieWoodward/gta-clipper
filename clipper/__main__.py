"""python -m clipper {process|post|analytics|learn|accounts}"""
import sys

from . import jobs, services

CMDS = {"process": jobs.process, "post": jobs.post, "analytics": jobs.analytics, "learn": jobs.learn,
        "accounts": lambda: print(services.blotato_accounts())}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CMDS:
        sys.exit(f"usage: python -m clipper [{'|'.join(CMDS)}]")
    CMDS[sys.argv[1]]()
