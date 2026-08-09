Set-StrictMode -Version Latest

if (-not ('ProblemLocator.E2E.NativeJob' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace ProblemLocator.E2E {
    [StructLayout(LayoutKind.Sequential)]
    public struct IO_COUNTERS {
        public UInt64 ReadOperationCount;
        public UInt64 WriteOperationCount;
        public UInt64 OtherOperationCount;
        public UInt64 ReadTransferCount;
        public UInt64 WriteTransferCount;
        public UInt64 OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public Int64 PerProcessUserTimeLimit;
        public Int64 PerJobUserTimeLimit;
        public UInt32 LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public UInt32 ActiveProcessLimit;
        public UIntPtr Affinity;
        public UInt32 PriorityClass;
        public UInt32 SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    public static class NativeJob {
        public const UInt32 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
        public const Int32 JobObjectExtendedLimitInformation = 9;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool SetInformationJobObject(
            IntPtr job,
            Int32 informationClass,
            IntPtr information,
            UInt32 informationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool TerminateJobObject(IntPtr job, UInt32 exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool CloseHandle(IntPtr handle);
    }
}
'@
}

$script:E2EUtf8 = New-Object System.Text.UTF8Encoding($false, $true)

function New-E2EKillOnCloseJob {
    $job = [ProblemLocator.E2E.NativeJob]::CreateJobObject([IntPtr]::Zero, $null)
    if ($job -eq [IntPtr]::Zero) {
        throw "E2E_JOB_CREATE_FAILED:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
    }

    $information = New-Object ProblemLocator.E2E.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    $information.BasicLimitInformation.LimitFlags = [ProblemLocator.E2E.NativeJob]::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    $size = [Runtime.InteropServices.Marshal]::SizeOf($information)
    $pointer = [Runtime.InteropServices.Marshal]::AllocHGlobal($size)
    try {
        [Runtime.InteropServices.Marshal]::StructureToPtr($information, $pointer, $false)
        if (-not [ProblemLocator.E2E.NativeJob]::SetInformationJobObject(
            $job,
            [ProblemLocator.E2E.NativeJob]::JobObjectExtendedLimitInformation,
            $pointer,
            [uint32]$size
        )) {
            throw "E2E_JOB_CONFIG_FAILED:$([Runtime.InteropServices.Marshal]::GetLastWin32Error())"
        }
    }
    catch {
        [void][ProblemLocator.E2E.NativeJob]::CloseHandle($job)
        throw
    }
    finally {
        [Runtime.InteropServices.Marshal]::FreeHGlobal($pointer)
    }
    return $job
}

function Write-E2EProcessText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text
    )
    [IO.File]::WriteAllText($Path, $Text, $script:E2EUtf8)
}

function Write-E2ETimeoutReceipt {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExecutableName,
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][long]$ElapsedMilliseconds,
        [Parameter(Mandatory = $true)][bool]$JobAssigned,
        [Parameter(Mandatory = $true)][bool]$ForcedTreeKill
    )
    $receipt = [ordered]@{
        schema_version = 1
        result = 'TIMEOUT'
        executable_name = $ExecutableName
        process_id = $ProcessId
        timeout_seconds = $TimeoutSeconds
        elapsed_milliseconds = $ElapsedMilliseconds
        job_assigned = $JobAssigned
        forced_tree_kill = $ForcedTreeKill
        arguments_recorded = $false
    }
    $json = $receipt | ConvertTo-Json -Compress -Depth 5
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = $script:E2EUtf8.GetBytes($json + "`n")
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
}

function Invoke-E2EBoundedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ArgumentLine,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$StdoutPath,
        [Parameter(Mandatory = $true)][string]$StderrPath,
        [Parameter(Mandatory = $true)][ValidateRange(1, 3600)][int]$TimeoutSeconds,
        [string]$TimeoutReceiptPath
    )

    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) { throw "E2E_EXECUTABLE_ABSENT:$FilePath" }
    if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) { throw "E2E_WORKDIR_ABSENT:$WorkingDirectory" }

    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $FilePath
    $start.Arguments = $ArgumentLine
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = $script:E2EUtf8
    $start.StandardErrorEncoding = $script:E2EUtf8

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    $job = [IntPtr]::Zero
    $jobAssigned = $false
    $forcedTreeKill = $false
    $stdoutTask = $null
    $stderrTask = $null
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    try {
        # Configure the Job before starting the child so even very short-lived
        # processes can be assigned before they exit.
        $job = New-E2EKillOnCloseJob
        if (-not $process.Start()) { throw "E2E_PROCESS_START_FAILED:$FilePath" }
        $processId = $process.Id
        $jobAssigned = [ProblemLocator.E2E.NativeJob]::AssignProcessToJobObject($job, $process.Handle)
        if (-not $jobAssigned) {
            $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            try { $process.Kill() } catch {}
            throw "E2E_JOB_ASSIGN_FAILED:${processId}:$errorCode"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()

        $completed = $process.WaitForExit([int]($TimeoutSeconds * 1000))
        if (-not $completed) {
            $forcedTreeKill = $true
            if (-not [ProblemLocator.E2E.NativeJob]::TerminateJobObject($job, 1460)) {
                $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                throw "E2E_JOB_TERMINATE_FAILED:${processId}:$errorCode"
            }
            if (-not $process.WaitForExit(5000)) { throw "E2E_PROCESS_TREE_STILL_RUNNING:$processId" }
            [void][ProblemLocator.E2E.NativeJob]::CloseHandle($job)
            $job = [IntPtr]::Zero
        }
        elseif ($job -ne [IntPtr]::Zero) {
            # The root exited normally. Closing the Job prevents detached MCP
            # descendants from holding output files or surviving the phase.
            [void][ProblemLocator.E2E.NativeJob]::CloseHandle($job)
            $job = [IntPtr]::Zero
        }

        [void][Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdoutTask, $stderrTask), 5000)
        $stdout = if ($stdoutTask.IsCompleted) { $stdoutTask.Result } else { '' }
        $stderr = if ($stderrTask.IsCompleted) { $stderrTask.Result } else { '' }
        Write-E2EProcessText -Path $StdoutPath -Text $stdout
        Write-E2EProcessText -Path $StderrPath -Text $stderr

        if (-not $completed) {
            if ([string]::IsNullOrWhiteSpace($TimeoutReceiptPath)) { $TimeoutReceiptPath = "$StdoutPath.timeout.json" }
            Write-E2ETimeoutReceipt -Path $TimeoutReceiptPath -ExecutableName ([IO.Path]::GetFileName($FilePath)) -ProcessId $processId -TimeoutSeconds $TimeoutSeconds -ElapsedMilliseconds $stopwatch.ElapsedMilliseconds -JobAssigned $jobAssigned -ForcedTreeKill $forcedTreeKill
            throw "E2E_PROCESS_TIMEOUT:$([IO.Path]::GetFileName($FilePath)):$TimeoutSeconds"
        }

        return [PSCustomObject][ordered]@{
            exit_code = $process.ExitCode
            process_id = $processId
            elapsed_milliseconds = $stopwatch.ElapsedMilliseconds
            timed_out = $false
            job_assigned = $jobAssigned
            forced_tree_kill = $forcedTreeKill
        }
    }
    finally {
        $stopwatch.Stop()
        if ($job -ne [IntPtr]::Zero) { [void][ProblemLocator.E2E.NativeJob]::CloseHandle($job) }
        $process.Dispose()
    }
}
