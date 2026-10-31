from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

_WINDOWS_ENV_ALLOWLIST = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "PSMODULEPATH",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
}

_POSIX_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TERM",
    "SHELL",
    "USER",
    "LOGNAME",
}

_SECRET_PREFIXES = (
    "OPENAI_",
    "GOOGLE_",
    "GEMINI_",
    "TAVILY_",
    "TELEGRAM_",
    "GITHUB_",
    "GH_",
    "AWS_",
    "AZURE_",
    "ANTHROPIC_",
    "DEEPSEEK_",
    "VOYAGE_",
    "MISTRAL_",
    "HF_",
    "HUGGINGFACE_",
)


# Variables that change which code a command runs. An approval shows the
# command, not its environment, so these would make "node --version" load
# any script unseen.
_CODE_LOADING_ENV_KEYS = {
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "PSMODULEPATH",
    "SYSTEMROOT",
    "WINDIR",
    "NODE_OPTIONS",
    "NODE_PATH",
    "PYTHONSTARTUP",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "BASH_ENV",
    "ENV",
    "PROMPT_COMMAND",
    "PERL5OPT",
    "PERL5LIB",
    "RUBYOPT",
    "RUBYLIB",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
    "GIT_EXEC_PATH",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
}
_CODE_LOADING_ENV_PREFIXES = ("LD_", "DYLD_", "GIT_CONFIG", "NPM_CONFIG_")


def is_code_loading_env_key(key: str) -> bool:
    normalized = _normalize_key(key).upper()
    return normalized in _CODE_LOADING_ENV_KEYS or normalized.startswith(_CODE_LOADING_ENV_PREFIXES)


@dataclass(slots=True)
class SanitizedEnvironment:
    env: dict[str, str]
    inherited_keys: list[str] = field(default_factory=list)
    explicit_keys: list[str] = field(default_factory=list)
    blocked_keys: list[str] = field(default_factory=list)
    secret_like_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False
    reason_code: str = ""
    reason: str = ""

    def metadata(self) -> dict:
        return {
            "env_inheritance": "scrubbed",
            "env_keys": sorted(self.env.keys(), key=str.upper),
            "inherited_env_keys": self.inherited_keys,
            "explicit_env_keys": self.explicit_keys,
            "blocked_env_keys": self.blocked_keys,
            "secret_like_env_keys": self.secret_like_keys,
            "warnings": self.warnings,
        }


def _normalize_key(key: str) -> str:
    return str(key or "").strip()


def is_secret_like_env_key(key: str) -> bool:
    normalized = _normalize_key(key).upper()
    if not normalized:
        return False
    return (
        "TOKEN" in normalized
        or "SECRET" in normalized
        or "PASSWORD" in normalized
        or "PRIVATE_KEY" in normalized
        or "API_KEY" in normalized
        or normalized.endswith("_KEY")
        or normalized.startswith(_SECRET_PREFIXES)
    )


def default_inherited_env_keys(*, os_name: str | None = None, shell: str = "") -> set[str]:
    name = os_name or os.name
    allowed = _WINDOWS_ENV_ALLOWLIST if name == "nt" else _POSIX_ENV_ALLOWLIST
    keys = set(allowed)
    if str(shell or "").lower() in {"powershell", "pwsh"}:
        keys.add("PSMODULEPATH")
    return keys


def _find_env_key(env: Mapping[str, str], wanted_upper: str) -> str | None:
    for key in env:
        if str(key).upper() == wanted_upper:
            return str(key)
    return None


def build_exec_environment(
    *,
    requested_env: Mapping[str, object] | None,
    shell: str,
    profile: str = "standard",
    backend: str = "local_direct",
    inherit_from: Mapping[str, str] | None = None,
    allow_explicit_secret_env: bool = False,
) -> SanitizedEnvironment:
    source_env = os.environ if inherit_from is None else inherit_from
    allowed_keys = set() if backend == "docker" else default_inherited_env_keys(shell=shell)
    env: dict[str, str] = {}
    inherited_keys: list[str] = []
    secret_like_keys: list[str] = []
    warnings: list[str] = []

    for allowed_key in sorted(allowed_keys):
        source_key = _find_env_key(source_env, allowed_key)
        if source_key is None or is_secret_like_env_key(source_key):
            if source_key and is_secret_like_env_key(source_key):
                secret_like_keys.append(source_key)
            continue
        env[source_key] = str(source_env[source_key])
        inherited_keys.append(source_key)

    explicit_keys: list[str] = []
    blocked_keys: list[str] = []
    code_loading_keys: list[str] = []
    for raw_key, raw_value in (requested_env or {}).items():
        key = _normalize_key(str(raw_key))
        if not key:
            continue
        if is_code_loading_env_key(key):
            code_loading_keys.append(key)
            continue
        if is_secret_like_env_key(key):
            blocked_keys.append(key)
            secret_like_keys.append(key)
            continue
        env[key] = str(raw_value)
        explicit_keys.append(key)

    if code_loading_keys:
        return SanitizedEnvironment(
            env=env,
            inherited_keys=sorted(inherited_keys, key=str.upper),
            explicit_keys=sorted(explicit_keys, key=str.upper),
            blocked_keys=sorted(set(code_loading_keys), key=str.upper),
            secret_like_keys=sorted(set(secret_like_keys), key=str.upper),
            warnings=warnings,
            blocked=True,
            reason_code="code_loading_env_not_supported",
            reason=(
                "Environment variables that change which code runs (such as PATH, NODE_OPTIONS or "
                "PYTHONPATH) are not supported. Put the setting in the command so it is shown for approval."
            ),
        )

    if blocked_keys and not allow_explicit_secret_env:
        return SanitizedEnvironment(
            env=env,
            inherited_keys=sorted(inherited_keys, key=str.upper),
            explicit_keys=sorted(explicit_keys, key=str.upper),
            blocked_keys=sorted(set(blocked_keys), key=str.upper),
            secret_like_keys=sorted(set(secret_like_keys), key=str.upper),
            warnings=warnings,
            blocked=True,
            reason_code="secret_env_not_supported",
            reason=(
                "Explicit secret-looking environment variables are not supported yet. "
                "Configure credentials through app settings or a future secure secret store."
            ),
        )

    return SanitizedEnvironment(
        env=env,
        inherited_keys=sorted(inherited_keys, key=str.upper),
        explicit_keys=sorted(explicit_keys, key=str.upper),
        blocked_keys=[],
        secret_like_keys=sorted(set(secret_like_keys), key=str.upper),
        warnings=warnings,
    )
