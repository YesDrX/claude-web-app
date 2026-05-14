"""CLI tool to manage Claude Code Web credentials.

Usage:
    python change_password.py                       # Interactive: change password
    python change_password.py <newpass>             # Set password directly
    python change_password.py --username <name>     # Change username
    python change_password.py --show                # Show current username (not password)
    python change_password.py --config path/to/config.toml

The password is hashed with PBKDF2-SHA256 before storage.
"""

import getpass
import re
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.toml"


def _read_config(config_path):
    return config_path.read_text("utf-8")


def _write_config(config_path, text):
    config_path.write_text(text, "utf-8")


def change_password(config_path: Path, new_password: str):
    if not config_path.exists():
        print(f"Error: config file not found at {config_path}")
        sys.exit(1)

    from auth import hash_password

    hashed = hash_password(new_password)
    content = _read_config(config_path)
    lines = content.split("\n")
    new_lines = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("password_hash") or stripped.startswith("password ="):
            new_lines.append(f'password_hash = "{hashed}"')
            found = True
        else:
            new_lines.append(line)

    if not found:
        for i, line in enumerate(new_lines):
            if line.strip() == "[auth]":
                new_lines.insert(i + 1, f'password_hash = "{hashed}"')
                found = True
                break

    _write_config(config_path, "\n".join(new_lines))
    print("Password updated successfully.")

    # Verify
    from auth import verify_password
    updated = _read_config(config_path)
    for line in updated.split("\n"):
        if "password_hash" in line:
            stored = line.split('"')[1]
            if verify_password(stored, new_password):
                print("  Verification: OK")
            else:
                print("  Verification: FAILED")
            break


def change_username(config_path: Path, new_username: str):
    if not config_path.exists():
        print(f"Error: config file not found at {config_path}")
        sys.exit(1)

    if not re.match(r'^[a-zA-Z0-9_.-]{1,64}$', new_username):
        print("Error: username must be 1-64 chars (letters, numbers, dots, hyphens, underscores).")
        sys.exit(1)

    content = _read_config(config_path)
    lines = content.split("\n")
    new_lines = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("username"):
            new_lines.append(f'username = "{new_username}"')
            found = True
        else:
            new_lines.append(line)

    if not found:
        for i, line in enumerate(new_lines):
            if line.strip() == "[auth]":
                new_lines.insert(i + 1, f'username = "{new_username}"')
                found = True
                break

    _write_config(config_path, "\n".join(new_lines))
    print("Username updated successfully.")
    print(f"  New username: {new_username}")


def show_current(config_path: Path):
    if not config_path.exists():
        print(f"Config not found at {config_path}")
        sys.exit(1)
    content = _read_config(config_path)
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("username"):
            print(f"Username: {stripped.split('=')[1].strip().strip('\"')}")
        if stripped.startswith("password_hash"):
            print("Password: <hashed with PBKDF2-SHA256>")
        if stripped.startswith("password ="):
            print("Password: <plaintext — run this tool to upgrade to a hash>")


def main():
    config_path = CONFIG_PATH
    args = sys.argv[1:]

    show = False
    new_username = None
    new_password = None

    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--config="):
            config_path = Path(a.split("=", 1)[1])
        elif a == "--config":
            i += 1
            config_path = Path(args[i])
        elif a in ("--username", "-u"):
            i += 1
            new_username = args[i]
        elif a in ("--password", "-p"):
            i += 1
            new_password = args[i]
        elif a in ("--help", "-h"):
            print(__doc__)
            sys.exit(0)
        elif a == "--show":
            show = True
        else:
            new_password = a
        i += 1

    if show:
        show_current(config_path)
        sys.exit(0)

    if new_username:
        change_username(config_path, new_username)
        print()  # blank line separator

    if new_password:
        change_password(config_path, new_password)
    elif new_username:
        # Only username was changed, we're done
        pass
    else:
        # No flags, no positional — interactive password change
        pw = getpass.getpass("New password: ")
        confirm = getpass.getpass("Confirm password: ")
        if pw != confirm:
            print("Error: passwords do not match.")
            sys.exit(1)
        if len(pw) < 4:
            print("Error: password must be at least 4 characters.")
            sys.exit(1)
        change_password(config_path, pw)


if __name__ == "__main__":
    main()
