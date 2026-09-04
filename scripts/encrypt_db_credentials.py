#!/usr/bin/env python3
"""Generate a Fernet key or encrypt database credentials for database.yaml."""

from __future__ import annotations

import argparse
import getpass
import os

from cryptography.fernet import Fernet

from morphlake.database import encrypt_secret


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Encrypt MorphLake database username and password for YAML configuration."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("generate-key", help="Generate a new MORPHLAKE_DB_CREDENTIAL_KEY")
    encrypt = subparsers.add_parser("encrypt", help="Prompt for credentials and print YAML values")
    encrypt.add_argument(
        "--key-env",
        default="MORPHLAKE_DB_CREDENTIAL_KEY",
        help="Environment variable containing the Fernet key",
    )
    return parser


def run() -> None:
    args = build_parser().parse_args()
    if args.command == "generate-key":
        print(Fernet.generate_key().decode("ascii"))
        return

    key = os.getenv(args.key_env)
    if not key:
        raise SystemExit(f"Environment variable {args.key_env} is not set")
    username = getpass.getpass("Database username: ")
    password = getpass.getpass("Database password: ")
    if not username or not password:
        raise SystemExit("Username and password cannot be empty")
    print("auth:")
    print("  mode: encrypt")
    print(f"  username: {encrypt_secret(username, key)}")
    print(f"  password: {encrypt_secret(password, key)}")
    print(f"  key_env: {args.key_env}")


if __name__ == "__main__":
    run()
