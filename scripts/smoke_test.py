"""Call each tool against a real engine and print the results.

Reads the same OLVM_* environment variables as the server. If neither
OLVM_PASSWORD nor OLVM_PASSWORD_FILE is set, prompts for the password.

    uv run python scripts/smoke_test.py [vm-name]
"""

import getpass
import json
import os
import sys

from mcp.server.mcpserver.exceptions import ToolError

from olvm_mcp import server


def show(title, fn, *args):
    print(f"\n== {title} ==")
    try:
        print(json.dumps(fn(*args), indent=2))
    except ToolError as e:
        print(f"ERROR: {e}")


def main() -> None:
    if not (os.environ.get("OLVM_PASSWORD") or os.environ.get("OLVM_PASSWORD_FILE")):
        os.environ["OLVM_PASSWORD"] = getpass.getpass(f"Password for {os.environ.get('OLVM_USERNAME')}: ")

    show("list_hosts", server.list_hosts)
    show("list_vms", server.list_vms)
    if len(sys.argv) > 1:
        show(f"get_vm {sys.argv[1]}", server.get_vm, sys.argv[1])


if __name__ == "__main__":
    main()
