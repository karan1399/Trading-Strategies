<#
.SYNOPSIS
    Antigravity Continuous Autonomous Wake-Up Daemon (PowerShell).
    Automatically activates the Antigravity window, pastes clipboard content, and hits Enter at the 0th and 30th minute of every hour.

.DESCRIPTION
    Runs in a continuous loop in the user's Windows desktop session.
    Calculates the exact sleep duration until the next :00 or :30 minute boundary.
    At trigger time:
      1. Finds and brings Antigravity to the foreground (restores if minimized).
      2. Waits for input focus.
      3. Pastes whatever content is on the Windows Clipboard (Ctrl+V).
      4. Sends Enter to trigger the prompt in Antigravity chat.
      5. Resumes sleeping until the next boundary.

.PARAMETER TestNow
    Runs an immediate test trigger (activates window, pastes clipboard, hits Enter) without waiting for the 30-min boundary.

.PARAMETER DefaultPromptIfEmpty
    If the clipboard is empty, loads this text (or file) as fallback so the trigger never sends a blank message.

.EXAMPLE
    # Run continuously (normal daemon mode):
    .\scripts\antigravity_auto_wake.ps1

.EXAMPLE
    # Test paste and Enter immediately:
    .\scripts\antigravity_auto_wake.ps1 -TestNow
#>

param(
    [switch]$TestNow,
    [switch]$DryRun,
    [string]$DefaultPrompt = "Autonomous Quant Desk Review: Execute full 16-parameter telemetry scan, audit blotter, and verify risk limits."
)

# 1. Load Windows Forms and Interaction assemblies for keystrokes and window management
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName Microsoft.VisualBasic

# 2. P/Invoke user32.dll + kernel32.dll for wildcard EnumWindows lookup + reliable foreground focus
$win32Type = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public class AgAutoWakeWin32V2 {
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);

    [DllImport("kernel32.dll")]
    public static extern uint GetCurrentThreadId();

    [DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc enumProc, IntPtr lParam);

    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    public static IntPtr MatchedHandle = IntPtr.Zero;
    public static string MatchedTitle = "";
    public static string MatchedProc = "";

    public static IntPtr FindAntigravityIdeWindow(string preferredWorkspace) {
        MatchedHandle = IntPtr.Zero;
        MatchedTitle = "";
        MatchedProc = "";
        IntPtr fallbackHandle = IntPtr.Zero;
        string fallbackTitle = "";
        string fallbackProc = "";

        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {
            if (!IsWindowVisible(hWnd)) return true;
            StringBuilder sb = new StringBuilder(512);
            GetWindowText(hWnd, sb, 512);
            string title = sb.ToString();
            if (string.IsNullOrEmpty(title)) return true;

            // Never target helper/sidecar tools (e.g., com.lbjlaq.antigravity-tools-siw)
            if (title.IndexOf("antigravity-tools", StringComparison.OrdinalIgnoreCase) >= 0 ||
                title.IndexOf("lbjlaq", StringComparison.OrdinalIgnoreCase) >= 0) {
                return true;
            }

            uint pid = 0;
            GetWindowThreadProcessId(hWnd, out pid);
            string procName = "";
            try {
                procName = System.Diagnostics.Process.GetProcessById((int)pid).ProcessName;
            } catch {}

            bool isIdeProc = procName.Equals("Antigravity", StringComparison.OrdinalIgnoreCase);
            bool looksLikeIdeTitle = title.IndexOf("- Antigravity", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                     title.Equals("Antigravity", StringComparison.OrdinalIgnoreCase);
            if (!isIdeProc && !looksLikeIdeTitle) return true;

            if (!string.IsNullOrEmpty(preferredWorkspace) &&
                title.IndexOf(preferredWorkspace, StringComparison.OrdinalIgnoreCase) >= 0) {
                MatchedHandle = hWnd;
                MatchedTitle = title;
                MatchedProc = procName;
                return false;
            }
            if (fallbackHandle == IntPtr.Zero) {
                fallbackHandle = hWnd;
                fallbackTitle = title;
                fallbackProc = procName;
            }
            return true;
        }, IntPtr.Zero);

        if (MatchedHandle == IntPtr.Zero && fallbackHandle != IntPtr.Zero) {
            MatchedHandle = fallbackHandle;
            MatchedTitle = fallbackTitle;
            MatchedProc = fallbackProc;
        }
        return MatchedHandle;
    }

    public static bool ForceForeground(IntPtr hWnd) {
        if (hWnd == IntPtr.Zero) return false;
        if (IsIconic(hWnd)) {
            ShowWindow(hWnd, 9); // SW_RESTORE
        }
        IntPtr fg = GetForegroundWindow();
        uint dummy;
        uint fgThread = (fg != IntPtr.Zero) ? GetWindowThreadProcessId(fg, out dummy) : 0;
        uint curThread = GetCurrentThreadId();
        bool attached = false;
        try {
            if (fgThread != 0 && fgThread != curThread) {
                attached = AttachThreadInput(curThread, fgThread, true);
            }
            BringWindowToTop(hWnd);
            return SetForegroundWindow(hWnd);
        } finally {
            if (attached) {
                AttachThreadInput(curThread, fgThread, false);
            }
        }
    }
}
"@

if (-not ('AgAutoWakeWin32V2' -as [type])) {
    Add-Type -TypeDefinition $win32Type -ErrorAction Stop
}

function Write-Log {
    param([string]$Message, [string]$Color = "White")
    $ts = [DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")
    Write-Host "[$ts] $Message" -ForegroundColor $Color
}

function Find-AntigravityHandle {
    $script:matchedTitle = ""
    $script:matchedProc = ""
    # Method A: Pure C# EnumWindows filtering on Antigravity.exe / "- Antigravity" (excludes antigravity-tools!)
    $hwnd = [AgAutoWakeWin32V2]::FindAntigravityIdeWindow("Trading-Strategies")
    if ($null -ne $hwnd -and $hwnd -is [IntPtr] -and $hwnd -ne [IntPtr]::Zero) {
        $script:matchedTitle = [AgAutoWakeWin32V2]::MatchedTitle
        $script:matchedProc = [AgAutoWakeWin32V2]::MatchedProc
        return $hwnd
    }

    # Method B: Exact Antigravity IDE Electron process scan (excludes antigravity-tools)
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -eq "Antigravity" -and $_.MainWindowTitle -notlike "*antigravity-tools*"
    }
    foreach ($p in $procs) {
        if ($null -ne $p.MainWindowHandle -and $p.MainWindowHandle -ne [IntPtr]::Zero) {
            $script:matchedTitle = $p.MainWindowTitle
            $script:matchedProc = $p.ProcessName
            return $p.MainWindowHandle
        }
    }
    return [IntPtr]::Zero
}

function Focus-AntigravityWindow {
    param([IntPtr]$TargetHandle)
    if ($null -eq $TargetHandle -or $TargetHandle -eq [IntPtr]::Zero) {
        return $false
    }
    $ok = [AgAutoWakeWin32V2]::ForceForeground($TargetHandle)
    Start-Sleep -Milliseconds 350
    if ($ok) { return $true }

    # Fallback: AppActivate by exact matched title
    if (-not [string]::IsNullOrWhiteSpace($script:matchedTitle)) {
        try {
            [Microsoft.VisualBasic.Interaction]::AppActivate($script:matchedTitle)
            Start-Sleep -Milliseconds 350
            return $true
        } catch {}
    }
    return $false
}

function Send-PasteAndEnter {
    # Read clipboard safely via PowerShell STA cmdlet; fall back to DefaultPrompt if empty or holding a file path/drop
    $fileDrop = Get-Clipboard -Format FileDropList -ErrorAction SilentlyContinue
    $clipboardText = Get-Clipboard -Raw -ErrorAction SilentlyContinue

    if ($fileDrop -or [string]::IsNullOrWhiteSpace($clipboardText) -or ($clipboardText.Trim() -match '^"?[A-Za-z]:\\')) {
        Write-Log "Clipboard was empty or held a file path. Using default wake prompt..." -Color Yellow
        if (-not $DryRun) {
            Set-Clipboard -Value $DefaultPrompt
        }
        $clipboardText = $DefaultPrompt
    }

    $preview = if ($clipboardText.Length -gt 60) { $clipboardText.Substring(0, 60).Replace("`r", "").Replace("`n", " ") + "..." } else { $clipboardText.Replace("`r", "").Replace("`n", " ") }
    Write-Log "Clipboard payload: '$preview'" -Color Cyan

    $targetHandle = Find-AntigravityHandle
    if ($DryRun) {
        Write-Log "DRY-RUN: Antigravity Window Handle=$targetHandle | Title='$script:matchedTitle'" -Color Yellow
        Write-Log "DRY-RUN: Would focus window, paste payload via Ctrl+V, and send {ENTER}." -Color Yellow
        return
    }

    Write-Log "Activating Antigravity IDE window (proc='$script:matchedProc' title='$script:matchedTitle')..." -Color Gray
    $focused = Focus-AntigravityWindow -TargetHandle $targetHandle

    if (-not $focused) {
        Write-Log "WARNING: Could not find or focus 'Antigravity' IDE window. Skipping blind keystrokes to avoid pasting into wrong app!" -Color Red
        return
    }
    $fgNow = [AgAutoWakeWin32V2]::GetForegroundWindow()
    if ($fgNow -ne $targetHandle) {
        Write-Log "WARNING: Foreground handle ($fgNow) != Antigravity handle ($targetHandle). Aborting paste for safety." -Color Red
        return
    }
    Write-Log "Antigravity IDE window verified in foreground." -Color Green

    # Small pause to ensure UI chat input field has focus (keep cursor in Antigravity chat box!)
    Start-Sleep -Milliseconds 450
    [System.Windows.Forms.SendKeys]::SendWait("^v")
    Write-Log "Sent Ctrl+V (Paste)" -Color Gray
    Start-Sleep -Milliseconds 600
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    Write-Log "Sent Enter! Prompt dispatched successfully to Antigravity." -Color Green
}

# --- MAIN EXECUTION ---
if (-not $DryRun) { Clear-Host }
Write-Host "==========================================================================" -ForegroundColor Cyan
Write-Host "     ANTIGRAVITY AUTONOMOUS WAKE-UP DAEMON (:00 & :30 OF EVERY HOUR)      " -ForegroundColor Yellow
Write-Host "==========================================================================" -ForegroundColor Cyan
Write-Host "Target Process : Antigravity.exe (excludes antigravity-tools sidecar)" -ForegroundColor Gray
Write-Host "Cadence        : Exact Minute :00:00 and :30:00" -ForegroundColor Gray
Write-Host "Action         : Activate Window -> Paste Clipboard -> Send Enter" -ForegroundColor Gray
Write-Host "Safety Note    : Leave cursor focused in the Antigravity chat box!" -ForegroundColor DarkYellow
Write-Host "Press Ctrl+C at any time to stop.`n" -ForegroundColor DarkGray

if ($TestNow -or $DryRun) {
    Write-Log "Running immediate execution ($(if ($DryRun) {'-DryRun'} else {'-TestNow'}))..." -Color Yellow
    Send-PasteAndEnter
    Write-Log "Completed." -Color Cyan
    exit 0
}

while ($true) {
    $now = [DateTime]::Now

    # Calculate next exact :00:00 or :30:00 boundary
    if ($now.Minute -lt 30) {
        $nextTarget = [DateTime]::new($now.Year, $now.Month, $now.Day, $now.Hour, 30, 0)
    } else {
        $nextTarget = [DateTime]::new($now.Year, $now.Month, $now.Day, $now.Hour, 0, 0).AddHours(1)
    }

    $waitSpan = $nextTarget - $now
    $sleepSecs = [Math]::Max(1, [int][Math]::Floor($waitSpan.TotalSeconds))

    Write-Log "Next trigger scheduled for: $($nextTarget.ToString('HH:mm:ss')) (in $([int]($sleepSecs / 60)) min $($sleepSecs % 60) sec)" -Color DarkCyan

    if ($sleepSecs -gt 1) {
        Start-Sleep -Seconds ($sleepSecs - 1)
    }
    # Fine alignment loop guarantees never firing before exact :00:00 / :30:00 boundary
    while ([DateTime]::Now -lt $nextTarget) {
        Start-Sleep -Milliseconds 200
    }

    Write-Log "=== TRIGGER TIME REACHED: $([DateTime]::Now.ToString('HH:mm:ss.fff')) ===" -Color Yellow
    Send-PasteAndEnter

    # Guard sleep past the trigger second so the next loop advances cleanly to +30 min
    Start-Sleep -Seconds 5
    Write-Host ""
}
