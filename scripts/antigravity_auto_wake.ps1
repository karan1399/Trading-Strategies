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
    [string]$DefaultPrompt = "Autonomous Quant Desk Review: Execute full 16-parameter telemetry scan, audit blotter, and verify risk limits."
)

# 1. Load Windows Forms and Interaction assemblies for keystrokes and window management
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName Microsoft.VisualBasic

# 2. P/Invoke user32.dll for rock-solid window foreground activation
$win32Type = @"
using System;
using System.Runtime.InteropServices;

public class Win32Window {
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
}
"@

try {
    Add-Type -TypeDefinition $win32Type -ErrorAction SilentlyContinue
} catch {
    # Type already defined in current session
}

function Write-Log {
    param([string]$Message, [string]$Color = "White")
    $ts = [DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")
    Write-Host "[$ts] $Message" -ForegroundColor $Color
}

function Focus-AntigravityWindow {
    # Method A: Search process list for Antigravity with a valid window handle
    $procs = Get-Process -Name "Antigravity" -ErrorAction SilentlyContinue
    $targetHandle = [IntPtr]::Zero

    foreach ($p in $procs) {
        if ($p.MainWindowHandle -ne [IntPtr]::Zero) {
            $targetHandle = $p.MainWindowHandle
            break
        }
    }

    if ($targetHandle -ne [IntPtr]::Zero) {
        # Restore window if minimized (SW_RESTORE = 9)
        if ([Win32Window]::IsIconic($targetHandle)) {
            [Win32Window]::ShowWindow($targetHandle, 9) | Out-Null
            Start-Sleep -Milliseconds 200
        }
        $success = [Win32Window]::SetForegroundWindow($targetHandle)
        Start-Sleep -Milliseconds 400
        if ($success) {
            return $true
        }
    }

    # Method B: Visual Basic AppActivate fallback (matches title or process)
    try {
        [Microsoft.VisualBasic.Interaction]::AppActivate("Antigravity")
        Start-Sleep -Milliseconds 400
        return $true
    } catch {
        # Try matching any window with Antigravity in title
        $allWindows = Get-Process | Where-Object { $_.MainWindowTitle -match "Antigravity" }
        if ($allWindows) {
            $h = $allWindows[0].MainWindowHandle
            if ($h -ne [IntPtr]::Zero) {
                [Win32Window]::ShowWindow($h, 9) | Out-Null
                [Win32Window]::SetForegroundWindow($h) | Out-Null
                Start-Sleep -Milliseconds 400
                return $true
            }
        }
        return $false
    }
}

function Send-PasteAndEnter {
    # Check clipboard content
    $clipboardText = [System.Windows.Forms.Clipboard]::GetText()
    
    if ([string]::IsNullOrWhiteSpace($clipboardText)) {
        Write-Log "Clipboard was empty. Using default wake prompt..." -Color Yellow
        [System.Windows.Forms.Clipboard]::SetText($DefaultPrompt)
        $clipboardText = $DefaultPrompt
    }

    $preview = if ($clipboardText.Length -gt 60) { $clipboardText.Substring(0, 60) + "..." } else { $clipboardText }
    Write-Log "Pasting content: '$preview'" -Color Cyan

    # Activate Antigravity Window
    Write-Log "Activating Antigravity window..." -Color Gray
    $focused = Focus-AntigravityWindow

    if (-not $focused) {
        Write-Log "WARNING: Could not find or focus 'Antigravity' window. Attempting keystrokes regardless." -Color Magenta
    } else {
        Write-Log "Antigravity window successfully brought to foreground." -Color Green
    }

    # Small pause to ensure UI chat input field has focus
    Start-Sleep -Milliseconds 500

    # Send Ctrl+V (Paste)
    [System.Windows.Forms.SendKeys]::SendWait("^v")
    Write-Log "Sent Ctrl+V (Paste)" -Color Gray

    # Pause 600ms before sending Enter so text settles in the input box
    Start-Sleep -Milliseconds 600

    # Send Enter ({ENTER})
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
    Write-Log "Sent Enter! Prompt dispatched successfully to Antigravity." -Color Green
}

# --- MAIN EXECUTION ---
Clear-Host
Write-Host "==========================================================================" -ForegroundColor Cyan
Write-Host "     ANTIGRAVITY AUTONOMOUS WAKE-UP DAEMON (:00 & :30 OF EVERY HOUR)      " -ForegroundColor Yellow
Write-Host "==========================================================================" -ForegroundColor Cyan
Write-Host "Target Process : Antigravity" -ForegroundColor Gray
Write-Host "Cadence        : Minute 00 and Minute 30" -ForegroundColor Gray
Write-Host "Action         : Activate Window -> Paste Clipboard -> Send Enter" -ForegroundColor Gray
Write-Host "Press Ctrl+C at any time to stop.`n" -ForegroundColor DarkGray

if ($TestNow) {
    Write-Log "Running immediate test trigger (-TestNow specified)..." -Color Yellow
    Send-PasteAndEnter
    Write-Log "Test trigger completed." -Color Cyan
    exit 0
}

while ($true) {
    $now = [DateTime]::Now
    
    # Calculate next :00 or :30 target
    if ($now.Minute -lt 30) {
        $nextTarget = [DateTime]::new($now.Year, $now.Month, $now.Day, $now.Hour, 30, 0)
    } else {
        $nextTarget = [DateTime]::new($now.Year, $now.Month, $now.Day, $now.Hour, 0, 0).AddHours(1)
    }

    $waitSpan = $nextTarget - $now
    $totalSeconds = [Math]::Max(1, [int]$waitSpan.TotalSeconds)

    Write-Log "Next trigger scheduled for: $($nextTarget.ToString('HH:mm:ss')) (in $([int]($totalSeconds / 60)) min $($totalSeconds % 60) sec)" -Color DarkCyan

    # Sleep until target time
    Start-Sleep -Seconds $totalSeconds

    Write-Log "=== TRIGGER TIME REACHED: $($nextTarget.ToString('HH:mm:ss')) ===" -Color Yellow
    
    # Execute Paste and Enter
    Send-PasteAndEnter

    # Guard sleep (8 seconds) to prevent triggering twice on the same second boundary
    Start-Sleep -Seconds 8
    Write-Host ""
}
