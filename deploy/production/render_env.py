#!/usr/bin/env python3
"""Render, update, check and print the production ``.env`` from ``.env.example``.

Standard library only; run with ``python3 -I``. Values are never printed except by
``print`` (which redacts every secret) and ``copy-secret`` (which writes one secret
straight into a new mode-0600 file).

    render_env.py create --example .env.example --out /srv/visioncast/app/<v>/.env --params FILE
    render_env.py update --example .env.example --out <existing .env>        --params FILE
    render_env.py check  --path <.env> --params FILE
    render_env.py print  --example .env.example [--existing <.env>] --params FILE   (redacted, stdout)
    render_env.py copy-secret --path <.env> --key INTEGRATION_MASTER_KEY --to /root/visioncast-secrets/integration-master-key

``--params FILE`` holds ``KEY=VALUE`` lines (no secrets). Secrets come from three
places only: fresh random values on ``create``, the existing ``.env`` on ``update``,
and ``--smtp-password-file`` (optional).

``create`` refuses to overwrite. ``update`` keeps every existing value (secrets and
operator edits), then applies the parameters over it, and writes atomically with
mode 0600. ``check`` verifies ownership, mode, that every required key is present
and that the managed values match the parameters; it names keys only.
"""
import argparse
import base64
import os
import re
import secrets
import sys
import tempfile

SECRET_KEYS = (
    "DJANGO_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "INTEGRATION_MASTER_KEY",
    "INTEGRATION_MASTER_KEY_PREVIOUS",
    "SMTP_PASSWORD",
    "BOOTSTRAP_ADMIN_PASSWORD",
)
GENERATED_KEYS = ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD", "INTEGRATION_MASTER_KEY")
REQUIRED_KEYS = GENERATED_KEYS + (
    "DJANGO_ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "PUBLIC_BASE_URL",
    "WEBINAR_IMAGE",
)
# Keys that must never carry a value in a deployed file.
FORBIDDEN_NON_EMPTY = ("DJANGO_TEST_SQLITE", "BOOTSTRAP_ADMIN_PASSWORD")
# docker compose reads .env for interpolation and env_file for the containers;
# this character set is parsed identically by both and by a POSIX shell.
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:,{}=@+%!*~^-]*$")
SMTP_PASSWORD_RE = re.compile(r"^[A-Za-z0-9_./:,{}=@+%!*~^-]{1,256}$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
HEADER = "# Managed by deploy/production/render_env.py (keys below were added by it)"


def die(msg):
    sys.exit(f"render_env: {msg}")


def read_kv(path):
    """KEY=VALUE lines; blank lines and # comments ignored. Values are not unquoted."""
    values = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not KEY_RE.match(key):
                continue
            values[key] = value.strip()
    return values


def load_params(path):
    params = read_kv(path)
    for key, value in params.items():
        if key in SECRET_KEYS:
            die(f"parameter file must not contain the secret key {key}")
        if not SAFE_VALUE_RE.match(value):
            die(f"parameter {key} contains characters outside the safe set")
    return params


def generated():
    return {
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "INTEGRATION_MASTER_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
    }


def read_secret_file(path):
    st = os.stat(path)
    if st.st_mode & 0o077:
        die(f"{path} must not be group/world readable (chmod 600)")
    with open(path, encoding="utf-8") as fh:
        value = fh.read().strip()
    if not SMTP_PASSWORD_RE.match(value):
        die(f"{path}: the password contains characters that compose/.env cannot carry safely "
            "(allowed: letters, digits and _ . / : , { } = @ + % ! * ~ ^ -); use a different relay credential")
    return value


def render(example_path, values):
    """Return the .env text: the example with every key in ``values`` set (active),
    commented ``# KEY=`` lines activated, and unknown keys appended under HEADER."""
    with open(example_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    remaining = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        m = re.match(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$", stripped)
        if m:
            key = m.group(2)
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
            if not m.group(1):
                out.append(f"{key}=")  # example value never carried over: every key is explicit
                continue
        out.append(line)
    if remaining:
        out.append("")
        out.append(HEADER)
        for key in remaining:
            out.append(f"{key}={remaining[key]}")
    return "\n".join(out) + "\n"


def example_defaults(example_path):
    """Active (uncommented) KEY=VALUE pairs of the example: the documented defaults."""
    return read_kv(example_path)


def validate(values):
    for key in REQUIRED_KEYS:
        if not values.get(key):
            die(f"{key} is required and empty")
    for key in FORBIDDEN_NON_EMPTY:
        if values.get(key):
            die(f"{key} must be empty in production")
    if values.get("DJANGO_DEBUG", "0") != "0":
        die("DJANGO_DEBUG must be 0")
    if values.get("EMAIL_DELIVERY", "disabled") == "smtp":
        if not values.get("SMTP_HOST"):
            die("EMAIL_DELIVERY=smtp needs SMTP_HOST")
        if (values.get("SMTP_STARTTLS", "1") == "1") == (values.get("SMTP_SSL", "0") == "1"):
            die("exactly one of SMTP_STARTTLS / SMTP_SSL must be 1")
    for key, value in values.items():
        if not SAFE_VALUE_RE.match(value):
            die(f"{key} contains characters outside the safe set")


def write_atomic(path, text, exclusive):
    parent = os.path.dirname(os.path.abspath(path)) or "."
    if exclusive:
        if os.path.lexists(path):
            die(f"{path} already exists; use update")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        return
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def compose_values(args, existing):
    values = dict(example_defaults(args.example))
    values.update(existing)
    fresh = generated()
    for key in GENERATED_KEYS:
        if not values.get(key):
            values[key] = fresh[key]
    values.update(load_params(args.params))
    if args.smtp_password_file:
        values["SMTP_PASSWORD"] = read_secret_file(args.smtp_password_file)
    values["BOOTSTRAP_ADMIN_PASSWORD"] = ""
    return values


def redact(text):
    out = []
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m and m.group(1) in SECRET_KEYS and m.group(2):
            out.append(f"{m.group(1)}=<redacted {len(m.group(2))} chars>")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def cmd_create(args):
    values = compose_values(args, {})
    validate(values)
    write_atomic(args.out, render(args.example, values), exclusive=True)
    print(f"created {args.out} (mode 0600, {len(values)} keys; values not shown)")


def cmd_update(args):
    if not os.path.isfile(args.out):
        die(f"{args.out} does not exist; use create")
    existing = read_kv(args.out)
    values = compose_values(args, existing)
    validate(values)
    changed = sorted(k for k in values if existing.get(k, "") != values[k] and k not in SECRET_KEYS)
    write_atomic(args.out, render(args.example, values), exclusive=False)
    print(f"updated {args.out}; changed keys: {', '.join(changed) if changed else 'none'} (values not shown)")


def cmd_print(args):
    existing = read_kv(args.existing) if args.existing and os.path.isfile(args.existing) else {}
    values = compose_values(args, existing)
    validate(values)
    sys.stdout.write(redact(render(args.example, values)))


def cmd_check(args):
    st = os.stat(args.path)
    problems = []
    if st.st_mode & 0o077:
        problems.append("file must be mode 0600")
    if st.st_uid != os.geteuid():
        problems.append("file must be owned by the deploying user")
    values = read_kv(args.path)
    for key in REQUIRED_KEYS:
        if not values.get(key):
            problems.append(f"{key} missing or empty")
    for key in FORBIDDEN_NON_EMPTY:
        if values.get(key):
            problems.append(f"{key} must be empty")
    for key, expected in load_params(args.params).items():
        if values.get(key, "") != expected:
            problems.append(f"{key} differs from the current parameters (re-run update)")
    if problems:
        sys.exit(f"{args.path}: " + "; ".join(problems))
    print(f"{args.path}: ok ({len(values)} keys; values not shown)")


def cmd_copy_secret(args):
    if args.key not in SECRET_KEYS:
        die(f"{args.key} is not a secret key")
    value = read_kv(args.path).get(args.key, "")
    if not value:
        die(f"{args.key} is empty in {args.path}")
    if os.path.lexists(args.to):
        print(f"exists, unchanged: {args.to}")
        return
    fd = os.open(args.to, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    print(f"saved (value not shown): {args.to}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("create", "update", "print"):
        p = sub.add_parser(name)
        p.add_argument("--example", required=True)
        p.add_argument("--params", required=True)
        p.add_argument("--smtp-password-file")
        if name == "print":
            p.add_argument("--existing")
        else:
            p.add_argument("--out", required=True)
    p = sub.add_parser("check")
    p.add_argument("--path", required=True)
    p.add_argument("--params", required=True)
    p = sub.add_parser("copy-secret")
    p.add_argument("--path", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--to", required=True)
    args = parser.parse_args()
    {"create": cmd_create, "update": cmd_update, "print": cmd_print,
     "check": cmd_check, "copy-secret": cmd_copy_secret}[args.cmd](args)


if __name__ == "__main__":
    main()
