param(
    [int]$TargetPid,
    [int]$Cast,
    [int]$Reel
)

$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

public static class RelayKeys
{
    [StructLayout(LayoutKind.Sequential)]
    private struct KeyboardInput
    {
        public ushort VirtualKey, ScanCode;
        public uint Flags, Time;
        public UIntPtr ExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MouseInput
    {
        public int X, Y;
        public uint Data, Flags, Time;
        public UIntPtr ExtraInfo;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputData
    {
        [FieldOffset(0)] public KeyboardInput Key;
        [FieldOffset(0)] public MouseInput Mouse;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Input
    {
        public uint Type;
        public InputData Data;
    }

    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    [DllImport("user32.dll")] private static extern IntPtr GetKeyboardLayout(uint threadId);
    [DllImport("user32.dll")] private static extern uint MapVirtualKeyEx(uint code, uint type, IntPtr layout);
    [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int key);
    [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint count, Input[] inputs, int size);

    public static string Run(Process target, int key, long deadline)
    {
        IntPtr window = GetForegroundWindow();
        uint processId;
        uint threadId = GetWindowThreadProcessId(window, out processId);
        if (target.HasExited) return "PAUSE The target application closed. Restart the helper with its new process ID.";
        if (threadId == 0 || processId != (uint)target.Id) return "PAUSE The target window is not in front.";
        if (GetForegroundWindow() != window) return "PAUSE The foreground window changed.";
        if (key == 0) return "OK";

        foreach (int modifier in new int[] { 0x10, 0x11, 0x12, 0x5B, 0x5C, key })
        {
            if ((GetAsyncKeyState(modifier) & 0x8000) != 0) return "PAUSE A key or modifier is already held.";
        }
        uint scan = MapVirtualKeyEx((uint)key, 4, GetKeyboardLayout(threadId));
        if (scan == 0 || (scan >> 8) > 0 && (scan >> 8) != 0xE0) return "ERROR Could not map the configured key.";
        uint flags = 0x0008 | ((scan >> 8) == 0xE0 ? 1u : 0u);
        Input down = new Input();
        down.Type = 1;
        down.Data.Key.ScanCode = (ushort)(scan & 0xFF);
        down.Data.Key.Flags = flags;
        Input up = down;
        up.Data.Key.Flags |= 0x0002;
        // Enqueue the keydown and keyup together so a helper shutdown cannot leave a key held.
        Input[] inputs = new Input[] { down, up };
        int size = Marshal.SizeOf(typeof(Input));
        if (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() >= deadline) return "PAUSE Command expired before input.";
        if (GetForegroundWindow() != window) return "PAUSE The target window lost focus before input.";
        uint sent = SendInput(2, inputs, size);
        if (sent == 1) SendInput(1, new Input[] { up }, size);
        return sent == 2 ? "OK" : "ERROR Windows rejected the key. Run the target application and the helper at the same privilege level.";
    }
}
"@

$target = [Diagnostics.Process]::GetProcessById($TargetPid)
# Keep a handle to the original process so a reused ID cannot authorize input.
$null = $target.Handle
$keys = @{ 'focus' = 0; 'cast' = $Cast; 'reel' = $Reel }
[Console]::WriteLine('READY')
while ($null -ne ($line = [Console]::ReadLine())) {
    $command, $deadline = $line.Split(' ', 2)
    try {
        [Console]::WriteLine([RelayKeys]::Run($target, $keys[$command], [long]$deadline))
    } catch {
        [Console]::WriteLine('ERROR Windows could not check the foreground or send the key.')
        [Console]::Error.WriteLine($_.Exception.Message)
    }
}
