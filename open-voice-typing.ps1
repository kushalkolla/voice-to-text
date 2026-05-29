# Opens Windows' built-in Voice Typing -- exactly the same as pressing Windows + H.
# Tip: click into your text box first, then run this, so your words land there.
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinH {
    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
"@
Start-Sleep -Milliseconds 400
$LWIN = 0x5B; $H = 0x48; $KEYUP = 0x0002
[WinH]::keybd_event($LWIN, 0, 0,      [UIntPtr]::Zero)
[WinH]::keybd_event($H,    0, 0,      [UIntPtr]::Zero)
[WinH]::keybd_event($H,    0, $KEYUP, [UIntPtr]::Zero)
[WinH]::keybd_event($LWIN, 0, $KEYUP, [UIntPtr]::Zero)
