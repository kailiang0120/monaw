from __future__ import annotations

import json
import os
import subprocess


_SETTINGS_PAGES = {
    "settings": "ms-settings:",
    "display": "ms-settings:display",
    "sound": "ms-settings:sound",
    "volume": "ms-settings:apps-volume",
    "brightness": "ms-settings:display",
    "bluetooth": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi",
    "network": "ms-settings:network",
    "power": "ms-settings:powersleep",
    "notifications": "ms-settings:notifications",
    "privacy": "ms-settings:privacy",
    "apps": "ms-settings:appsfeatures",
    "default_apps": "ms-settings:defaultapps",
    "date_time": "ms-settings:dateandtime",
}


def _json_error(message: str, *, reason_code: str = "system_control_error") -> str:
    return json.dumps({"status": "error", "error": message, "reason_code": reason_code}, ensure_ascii=False)


def _windows_only() -> str | None:
    if os.name != "nt":
        return _json_error("This system control tool is only supported on Windows.", reason_code="unsupported_os")
    return None


def _clamp_percent(value: int | float) -> int:
    return max(0, min(100, int(value)))


def _run_powershell_json(script: str, timeout: int = 10) -> dict:
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    stdout = (completed.stdout or "").strip()
    if completed.returncode != 0:
        error = (completed.stderr or stdout or f"PowerShell exited with {completed.returncode}").strip()
        return {"status": "error", "error": error, "reason_code": "powershell_failed"}
    if not stdout:
        return {"status": "error", "error": "PowerShell returned no output.", "reason_code": "empty_output"}
    try:
        decoded = json.loads(stdout)
    except json.JSONDecodeError:
        return {"status": "error", "error": stdout, "reason_code": "invalid_json_output"}
    return decoded if isinstance(decoded, dict) else {"status": "ok", "result": decoded}


def _volume_script(action: str, level: int, step: int) -> str:
    action_json = json.dumps(action)
    level_value = _clamp_percent(level)
    step_value = max(1, min(100, int(step or 5)))
    return f"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

namespace AgentSystemAudio {{
    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    public class MMDeviceEnumeratorComObject {{}}

    public enum EDataFlow {{ eRender = 0, eCapture = 1, eAll = 2 }}
    public enum ERole {{ eConsole = 0, eMultimedia = 1, eCommunications = 2 }}

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IMMDeviceEnumerator {{
        int EnumAudioEndpoints(EDataFlow dataFlow, int dwStateMask, IntPtr ppDevices);
        int GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, out IMMDevice ppDevice);
    }}

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IMMDevice {{
        int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, out IAudioEndpointVolume ppInterface);
    }}

    [ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IAudioEndpointVolume {{
        int RegisterControlChangeNotify(IntPtr pNotify);
        int UnregisterControlChangeNotify(IntPtr pNotify);
        int GetChannelCount(out uint pnChannelCount);
        int SetMasterVolumeLevel(float fLevelDB, Guid pguidEventContext);
        int SetMasterVolumeLevelScalar(float fLevel, Guid pguidEventContext);
        int GetMasterVolumeLevel(out float pfLevelDB);
        int GetMasterVolumeLevelScalar(out float pfLevel);
        int SetChannelVolumeLevel(uint nChannel, float fLevelDB, Guid pguidEventContext);
        int SetChannelVolumeLevelScalar(uint nChannel, float fLevel, Guid pguidEventContext);
        int GetChannelVolumeLevel(uint nChannel, out float pfLevelDB);
        int GetChannelVolumeLevelScalar(uint nChannel, out float pfLevel);
        int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, Guid pguidEventContext);
        int GetMute(out bool pbMute);
    }}

    public static class Audio {{
        private static IAudioEndpointVolume Endpoint() {{
            IMMDeviceEnumerator enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
            IMMDevice device;
            Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender, ERole.eMultimedia, out device));
            Guid endpointVolumeId = typeof(IAudioEndpointVolume).GUID;
            IAudioEndpointVolume endpoint;
            Marshal.ThrowExceptionForHR(device.Activate(ref endpointVolumeId, 23, IntPtr.Zero, out endpoint));
            return endpoint;
        }}

        public static float GetVolume() {{
            float volume;
            Marshal.ThrowExceptionForHR(Endpoint().GetMasterVolumeLevelScalar(out volume));
            return volume;
        }}

        public static bool GetMute() {{
            bool muted;
            Marshal.ThrowExceptionForHR(Endpoint().GetMute(out muted));
            return muted;
        }}

        public static void SetVolume(float value) {{
            Marshal.ThrowExceptionForHR(Endpoint().SetMasterVolumeLevelScalar(Math.Max(0, Math.Min(1, value)), Guid.Empty));
        }}

        public static void SetMute(bool value) {{
            Marshal.ThrowExceptionForHR(Endpoint().SetMute(value, Guid.Empty));
        }}
    }}
}}
"@
$action = {action_json}
$level = {level_value}
$step = {step_value}
$current = [int][Math]::Round([AgentSystemAudio.Audio]::GetVolume() * 100)
$muted = [AgentSystemAudio.Audio]::GetMute()
switch ($action) {{
    'set' {{ [AgentSystemAudio.Audio]::SetVolume($level / 100.0); $current = $level }}
    'increase' {{ $current = [Math]::Min(100, $current + $step); [AgentSystemAudio.Audio]::SetVolume($current / 100.0) }}
    'decrease' {{ $current = [Math]::Max(0, $current - $step); [AgentSystemAudio.Audio]::SetVolume($current / 100.0) }}
    'mute' {{ [AgentSystemAudio.Audio]::SetMute($true); $muted = $true }}
    'unmute' {{ [AgentSystemAudio.Audio]::SetMute($false); $muted = $false }}
    'toggle_mute' {{ $muted = -not $muted; [AgentSystemAudio.Audio]::SetMute($muted) }}
    'get' {{ }}
    default {{ throw "Unsupported volume action: $action" }}
}}
$current = [int][Math]::Round([AgentSystemAudio.Audio]::GetVolume() * 100)
$muted = [AgentSystemAudio.Audio]::GetMute()
[pscustomobject]@{{ status = 'ok'; action = $action; level = $current; muted = $muted }} | ConvertTo-Json -Compress
"""


def system_volume(action: str = "get", level: int = 50, step: int = 5) -> str:
    unsupported = _windows_only()
    if unsupported:
        return unsupported
    normalized = str(action or "get").strip().lower()
    if normalized not in {"get", "set", "increase", "decrease", "mute", "unmute", "toggle_mute"}:
        return _json_error(f"Unsupported volume action: {action}", reason_code="invalid_action")
    return json.dumps(
        _run_powershell_json(_volume_script(normalized, level, step)),
        ensure_ascii=False,
    )


def _brightness_script(action: str, level: int, step: int) -> str:
    action_json = json.dumps(action)
    level_value = _clamp_percent(level)
    step_value = max(1, min(100, int(step or 10)))
    return f"""
$ErrorActionPreference = 'Stop'
$action = {action_json}
$level = {level_value}
$step = {step_value}
$brightness = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -First 1
if ($null -eq $brightness) {{ throw 'No controllable display brightness endpoint was found.' }}
$current = [int]$brightness.CurrentBrightness
if ($action -eq 'set') {{
    $target = $level
}} elseif ($action -eq 'increase') {{
    $target = [Math]::Min(100, $current + $step)
}} elseif ($action -eq 'decrease') {{
    $target = [Math]::Max(0, $current - $step)
}} elseif ($action -eq 'get') {{
    $target = $current
}} else {{
    throw "Unsupported brightness action: $action"
}}
if ($action -ne 'get') {{
    $methods = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | Select-Object -First 1
    if ($null -eq $methods) {{ throw 'No brightness control method was found.' }}
    Invoke-CimMethod -InputObject $methods -MethodName WmiSetBrightness -Arguments @{{ Timeout = 1; Brightness = $target }} | Out-Null
}}
[pscustomobject]@{{ status = 'ok'; action = $action; level = $target }} | ConvertTo-Json -Compress
"""


def screen_brightness(action: str = "get", level: int = 50, step: int = 10) -> str:
    unsupported = _windows_only()
    if unsupported:
        return unsupported
    normalized = str(action or "get").strip().lower()
    if normalized not in {"get", "set", "increase", "decrease"}:
        return _json_error(f"Unsupported brightness action: {action}", reason_code="invalid_action")
    return json.dumps(
        _run_powershell_json(_brightness_script(normalized, level, step)),
        ensure_ascii=False,
    )


def desktop_configuration(action: str = "open", page: str = "settings") -> str:
    normalized_action = str(action or "open").strip().lower()
    normalized_page = str(page or "settings").strip().lower()
    if normalized_action == "list_pages":
        return json.dumps({"status": "ok", "pages": sorted(_SETTINGS_PAGES)}, ensure_ascii=False)
    unsupported = _windows_only()
    if unsupported:
        return unsupported
    if normalized_action != "open":
        return _json_error(f"Unsupported desktop configuration action: {action}", reason_code="invalid_action")
    uri = _SETTINGS_PAGES.get(normalized_page)
    if uri is None:
        return _json_error(
            f"Unsupported settings page: {page}",
            reason_code="invalid_page",
        )
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
    except Exception as exc:
        return _json_error(str(exc))
    return json.dumps({"status": "ok", "action": "open", "page": normalized_page, "uri": uri}, ensure_ascii=False)
