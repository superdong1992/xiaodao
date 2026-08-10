param(
    [Parameter(Mandatory = $true)][string]$SpecPath,
    [Parameter(Mandatory = $true)][string]$StatusPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false, $true)

if (-not ('ProblemLocator.TestFlow.NativeJob' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
namespace ProblemLocator.TestFlow {
    [StructLayout(LayoutKind.Sequential)] public struct IO_COUNTERS {
        public UInt64 ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public UInt64 ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)] public struct BASIC_LIMITS {
        public Int64 PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public UInt32 LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public UInt32 ActiveProcessLimit;
        public UIntPtr Affinity;
        public UInt32 PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)] public struct EXTENDED_LIMITS {
        public BASIC_LIMITS BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }
    public static class NativeJob {
        public const UInt32 KILL_ON_CLOSE = 0x00002000;
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError=true)] [return: MarshalAs(UnmanagedType.Bool)] public static extern bool SetInformationJobObject(IntPtr job, Int32 kind, IntPtr value, UInt32 length);
        [DllImport("kernel32.dll", SetLastError=true)] [return: MarshalAs(UnmanagedType.Bool)] public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError=true)] [return: MarshalAs(UnmanagedType.Bool)] public static extern bool TerminateJobObject(IntPtr job, UInt32 exitCode);
        [DllImport("kernel32.dll", SetLastError=true)] [return: MarshalAs(UnmanagedType.Bool)] public static extern bool CloseHandle(IntPtr handle);

        public sealed class PumpState {
            public Int32 Exceeded;
        }

        public static async Task<Int64> PumpAsync(Stream input, Stream output, Int64 limit, PumpState state) {
            byte[] buffer = new byte[8192];
            Int64 total = 0;
            for (;;) {
                Int32 count = await input.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
                if (count == 0) break;
                Int64 room = limit - total;
                Int32 accepted = room > 0 ? (Int32)Math.Min((Int64)count, room) : 0;
                if (accepted > 0) {
                    await output.WriteAsync(buffer, 0, accepted).ConfigureAwait(false);
                    await output.FlushAsync().ConfigureAwait(false);
                    total += accepted;
                }
                if (accepted != count) {
                    Interlocked.Exchange(ref state.Exceeded, 1);
                    break;
                }
            }
            await output.FlushAsync().ConfigureAwait(false);
            return total;
        }
    }
}
'@
}

function Quote-NativeArgument([string]$Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes += 1; continue }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($slashes * 2) + 1)))
            [void]$builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) { [void]$builder.Append(('\' * $slashes)); $slashes = 0 }
        [void]$builder.Append($character)
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * ($slashes * 2))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function New-KillOnCloseJob {
    $job = [ProblemLocator.TestFlow.NativeJob]::CreateJobObject([IntPtr]::Zero, $null)
    if ($job -eq [IntPtr]::Zero) { throw "TEST_FLOW_JOB_CREATE:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())" }
    $limits = New-Object ProblemLocator.TestFlow.EXTENDED_LIMITS
    $limits.BasicLimitInformation.LimitFlags = [ProblemLocator.TestFlow.NativeJob]::KILL_ON_CLOSE
    $size = [Runtime.InteropServices.Marshal]::SizeOf($limits)
    $pointer = [Runtime.InteropServices.Marshal]::AllocHGlobal($size)
    try {
        [Runtime.InteropServices.Marshal]::StructureToPtr($limits, $pointer, $false)
        if (-not [ProblemLocator.TestFlow.NativeJob]::SetInformationJobObject($job, 9, $pointer, [uint32]$size)) {
            throw "TEST_FLOW_JOB_CONFIG:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
        }
    }
    catch { [void][ProblemLocator.TestFlow.NativeJob]::CloseHandle($job); throw }
    finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($pointer) }
    return $job
}

$spec = [IO.File]::ReadAllText([IO.Path]::GetFullPath($SpecPath), $utf8) | ConvertFrom-Json
$stdoutStream = $null
$stderrStream = $null
$stdoutPump = $null
$stderrPump = $null
$process = $null
$job = [IntPtr]::Zero
$started = [DateTime]::UtcNow
$streamLimit = [int64]$spec.raw_log_limit_bytes
if ($streamLimit -le 0) { throw 'TEST_FLOW_RAW_LOG_LIMIT' }
$cancelPath = [IO.Path]::GetFullPath([string]$spec.cancel_path)
$pumpState = New-Object 'ProblemLocator.TestFlow.NativeJob+PumpState'
$controllerTermination = $false
try {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = [string]$spec.executable
    $start.Arguments = (@($spec.arguments) | ForEach-Object { Quote-NativeArgument ([string]$_) }) -join ' '
    $start.WorkingDirectory = [string]$spec.working_directory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = $utf8
    $start.StandardErrorEncoding = $utf8
    foreach ($property in @($spec.environment.PSObject.Properties)) {
        $start.EnvironmentVariables[[string]$property.Name] = [string]$property.Value
    }

    $stdoutStream = [IO.File]::Open([string]$spec.stdout_path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $stderrStream = [IO.File]::Open([string]$spec.stderr_path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    $job = New-KillOnCloseJob
    if (-not $process.Start()) { throw 'TEST_FLOW_PROCESS_START' }
    if (-not [ProblemLocator.TestFlow.NativeJob]::AssignProcessToJobObject($job, $process.Handle)) {
        throw "TEST_FLOW_JOB_ASSIGN:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
    }
    $stdoutPump = [ProblemLocator.TestFlow.NativeJob]::PumpAsync($process.StandardOutput.BaseStream, $stdoutStream, $streamLimit, $pumpState)
    $stderrPump = [ProblemLocator.TestFlow.NativeJob]::PumpAsync($process.StandardError.BaseStream, $stderrStream, $streamLimit, $pumpState)
    while (-not $process.WaitForExit(100)) {
        if ($pumpState.Exceeded -ne 0 -or [IO.File]::Exists($cancelPath)) {
            $controllerTermination = [IO.File]::Exists($cancelPath)
            if (-not [ProblemLocator.TestFlow.NativeJob]::TerminateJobObject($job, 1)) {
                throw "TEST_FLOW_JOB_TERMINATE:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
            }
            break
        }
    }
    if (-not $process.WaitForExit(5000)) {
        $process.Kill()
        if (-not $process.WaitForExit(5000)) { throw 'TEST_FLOW_PROCESS_TERMINATE_TIMEOUT' }
    }
    [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdoutPump, $stderrPump))
    $stdoutStream.Flush($true)
    $stderrStream.Flush($true)
    $stdoutStream.Dispose()
    $stdoutStream = $null
    $stderrStream.Dispose()
    $stderrStream = $null
    $status = [ordered]@{
        schema_version = 1
        status = 'EXITED'
        exit_code = $process.ExitCode
        process_id = $process.Id
        job_assigned = $true
        raw_log_limit_exceeded = ($pumpState.Exceeded -ne 0)
        controller_termination = $controllerTermination
        elapsed_milliseconds = [int64](([DateTime]::UtcNow - $started).TotalMilliseconds)
    }
    $stream = [IO.File]::Open([IO.Path]::GetFullPath($StatusPath), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = $utf8.GetBytes((($status | ConvertTo-Json -Compress) + "`n"))
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }
    exit $process.ExitCode
}
finally {
    if ($null -ne $stdoutStream) { $stdoutStream.Dispose() }
    if ($null -ne $stderrStream) { $stderrStream.Dispose() }
    if ($job -ne [IntPtr]::Zero) { [void][ProblemLocator.TestFlow.NativeJob]::CloseHandle($job) }
    if ($null -ne $process) { $process.Dispose() }
}
