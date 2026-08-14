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
using System.Runtime.InteropServices;
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
$process = $null
$job = [IntPtr]::Zero
$started = [DateTime]::UtcNow
$streamLimit = [int64]$spec.raw_log_limit_bytes
if ($streamLimit -le 0) { throw 'TEST_FLOW_RAW_LOG_LIMIT' }
$terminationPath = [IO.Path]::GetFullPath([string]$spec.termination_path)
$streamState = @{
    stdout_bytes = [int64]0
    stderr_bytes = [int64]0
    exceeded = $false
}
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
    $start.EnvironmentVariables.Clear()
    foreach ($property in @($spec.environment.PSObject.Properties)) {
        $start.EnvironmentVariables[[string]$property.Name] = [string]$property.Value
    }

    $stdoutStream = [IO.File]::Open(
        [IO.Path]::GetFullPath([string]$spec.stdout_path),
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::Read
    )
    $stderrStream = [IO.File]::Open(
        [IO.Path]::GetFullPath([string]$spec.stderr_path),
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::Read
    )
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    $job = New-KillOnCloseJob
    if (-not $process.Start()) { throw 'TEST_FLOW_PROCESS_START' }
    if (-not [ProblemLocator.TestFlow.NativeJob]::AssignProcessToJobObject($job, $process.Handle)) {
        throw "TEST_FLOW_JOB_ASSIGN:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
    }
    $bufferSize = 65536
    $stdoutBuffer = New-Object byte[] $bufferSize
    $stderrBuffer = New-Object byte[] $bufferSize
    $stdoutRead = $process.StandardOutput.BaseStream.ReadAsync($stdoutBuffer, 0, $stdoutBuffer.Length)
    $stderrRead = $process.StandardError.BaseStream.ReadAsync($stderrBuffer, 0, $stderrBuffer.Length)
    $stdoutDone = $false
    $stderrDone = $false
    $limitTriggered = $false
    $externalTerminationTriggered = $false
    while (-not ($process.HasExited -and $stdoutDone -and $stderrDone)) {
        if (-not $stdoutDone -and $stdoutRead.IsCompleted) {
            $stdoutCount = [int]$stdoutRead.GetAwaiter().GetResult()
            if ($stdoutCount -eq 0) {
                $stdoutDone = $true
            }
            else {
                $stdoutRemaining = $streamLimit - [int64]$streamState.stdout_bytes
                $stdoutWriteCount = $stdoutCount
                if ($stdoutRemaining -lt $stdoutWriteCount) {
                    $stdoutWriteCount = [int][Math]::Max([int64]0, $stdoutRemaining)
                }
                if ($stdoutWriteCount -gt 0) {
                    $stdoutStream.Write($stdoutBuffer, 0, $stdoutWriteCount)
                    $stdoutStream.Flush()
                    $streamState.stdout_bytes += [int64]$stdoutWriteCount
                }
                if ($stdoutWriteCount -lt $stdoutCount) {
                    $streamState.exceeded = $true
                    $stdoutDone = $true
                }
                else {
                    $stdoutRead = $process.StandardOutput.BaseStream.ReadAsync($stdoutBuffer, 0, $stdoutBuffer.Length)
                }
            }
        }
        if (-not $stderrDone -and $stderrRead.IsCompleted) {
            $stderrCount = [int]$stderrRead.GetAwaiter().GetResult()
            if ($stderrCount -eq 0) {
                $stderrDone = $true
            }
            else {
                $stderrRemaining = $streamLimit - [int64]$streamState.stderr_bytes
                $stderrWriteCount = $stderrCount
                if ($stderrRemaining -lt $stderrWriteCount) {
                    $stderrWriteCount = [int][Math]::Max([int64]0, $stderrRemaining)
                }
                if ($stderrWriteCount -gt 0) {
                    $stderrStream.Write($stderrBuffer, 0, $stderrWriteCount)
                    $stderrStream.Flush()
                    $streamState.stderr_bytes += [int64]$stderrWriteCount
                }
                if ($stderrWriteCount -lt $stderrCount) {
                    $streamState.exceeded = $true
                    $stderrDone = $true
                }
                else {
                    $stderrRead = $process.StandardError.BaseStream.ReadAsync($stderrBuffer, 0, $stderrBuffer.Length)
                }
            }
        }
        if ([bool]$streamState.exceeded -and $job -ne [IntPtr]::Zero) {
            if (-not [ProblemLocator.TestFlow.NativeJob]::TerminateJobObject($job, 1)) {
                throw "TEST_FLOW_JOB_TERMINATE:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
            }
            if (-not [ProblemLocator.TestFlow.NativeJob]::CloseHandle($job)) {
                throw "TEST_FLOW_JOB_CLOSE:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
            }
            $job = [IntPtr]::Zero
            $limitTriggered = $true
            break
        }
        if ([IO.File]::Exists($terminationPath) -and $job -ne [IntPtr]::Zero) {
            if (-not [ProblemLocator.TestFlow.NativeJob]::TerminateJobObject($job, 1)) {
                throw "TEST_FLOW_JOB_TERMINATE:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
            }
            if (-not [ProblemLocator.TestFlow.NativeJob]::CloseHandle($job)) {
                throw "TEST_FLOW_JOB_CLOSE:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
            }
            $job = [IntPtr]::Zero
            $externalTerminationTriggered = $true
            break
        }
        if (-not $process.HasExited) { [void]$process.WaitForExit(10) }
        else { [Threading.Thread]::Sleep(10) }
    }
    if ($limitTriggered -or $externalTerminationTriggered) {
        if (-not $process.WaitForExit(5000)) { throw 'TEST_FLOW_JOB_TERMINATION_TIMEOUT' }
    }
    else {
        $process.WaitForExit()
    }
    $stdoutStream.Flush($true)
    $stderrStream.Flush($true)
    $status = [ordered]@{
        schema_version = 1
        status = 'EXITED'
        exit_code = $process.ExitCode
        process_id = $process.Id
        job_assigned = $true
        raw_log_limit_exceeded = [bool]$streamState.exceeded
        external_termination_requested = [bool]$externalTerminationTriggered
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
