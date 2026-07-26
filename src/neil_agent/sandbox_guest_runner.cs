using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

internal static class SandboxGuestRunner
{
    private const int ProtocolVersion = 1;
    private const int RunnerVersion = 1;
    private const string SecurityAssurance = "candidate-job-only-not-certified";

    private const string ControlRoot = @"C:\NeilAgent\Control";
    private const string SnapshotRoot = @"C:\NeilAgent\Snapshot";
    private const string ScratchRoot = @"C:\NeilAgent\Scratch";
    private const string ResultRoot = @"C:\NeilAgent\Result";
    private const string ExportRoot = @"C:\NeilAgent\Export";
    private const string RequestPath = @"C:\NeilAgent\Control\request.json";
    private const string CancelPath = @"C:\NeilAgent\Control\cancel.signal";
    private const string ResultPath = @"C:\NeilAgent\Result\result.json";
    private const string MarkerPath = @"C:\NeilAgent\Result\complete.marker";
    private const string ExportPath = @"C:\NeilAgent\Export\result.json";

    private const int MaxRequestBytes = 64 * 1024;
    private const int MaxResultBytes = 2 * 1024 * 1024;
    private const int MaxArguments = 64;
    private const int MaxArgumentChars = 4096;
    private const int MaxEnvironmentItems = 16;
    private const int MaxEnvironmentValueChars = 4096;
    private const int MaxRelativePathChars = 240;
    private const int MinTimeoutMs = 100;
    private const int MaxTimeoutMs = 120000;
    private const int MinOutputBytes = 1024;
    private const int MaxOutputBytes = 1000000;
    private const long MinMemoryBytes = 16L * 1024L * 1024L;
    private const long MaxProcessMemoryBytes = 512L * 1024L * 1024L;
    private const long MaxJobMemoryBytes = 1024L * 1024L * 1024L;
    private const int MaxActiveProcesses = 16;

    private const uint CreateSuspended = 0x00000004;
    private const uint CreateNoWindow = 0x08000000;
    private const uint CreateUnicodeEnvironment = 0x00000400;
    private const uint ExtendedStartupInfoPresent = 0x00080000;
    private const uint StartfUseStdHandles = 0x00000100;
    private const uint HandleFlagInherit = 0x00000001;
    private const uint GenericRead = 0x80000000;
    private const uint OpenExisting = 3;
    private const uint FileAttributeNormal = 0x00000080;
    private const uint Infinite = 0xFFFFFFFF;
    private const uint WaitObject0 = 0;
    private const uint WaitTimeout = 258;
    private const uint JobObjectLimitActiveProcess = 0x00000008;
    private const uint JobObjectLimitProcessMemory = 0x00000100;
    private const uint JobObjectLimitJobMemory = 0x00000200;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint MoveFileWriteThrough = 0x00000008;
    private const uint ProcThreadAttributeHandleList = 0x00020002;
    private const int ErrorAlreadyExists = 183;
    private static readonly IntPtr InvalidHandleValue = new IntPtr(-1);

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length != 1)
            {
                return 64;
            }
            if (String.Equals(args[0], "execute", StringComparison.Ordinal))
            {
                return Execute();
            }
            if (String.Equals(args[0], "export", StringComparison.Ordinal))
            {
                return Export();
            }
            return 64;
        }
        catch
        {
            return 70;
        }
    }

    private static int Execute()
    {
        EnsureSafeDirectory(ControlRoot, false);
        EnsureSafeDirectory(SnapshotRoot, false);
        EnsureSafeDirectory(ScratchRoot, true);
        EnsureSafeDirectory(ResultRoot, true);
        EnsureAbsent(ResultPath);
        EnsureAbsent(MarkerPath);

        byte[] requestBytes = ReadSafeFile(RequestPath, MaxRequestBytes);
        GuestRequest request;
        try
        {
            request = GuestRequest.Parse(requestBytes);
        }
        catch
        {
            return 65;
        }

        GuestResult result;
        try
        {
            result = RunChild(request);
        }
        catch
        {
            result = GuestResult.Failure(
                request,
                "runner_error",
                "runner_failure",
                null,
                new byte[0],
                new byte[0],
                0,
                false);
        }

        byte[] resultBytes = result.CanonicalBytes();
        if (resultBytes.Length > MaxResultBytes)
        {
            return 70;
        }
        WriteAtomic(ResultRoot, ResultPath, resultBytes);
        string marker = request.RunId + "\n"
            + request.RequestHash + "\n"
            + request.InstanceId + "\n"
            + result.ResultHash + "\n";
        WriteAtomic(
            ResultRoot,
            MarkerPath,
            new UTF8Encoding(false, true).GetBytes(marker));
        return result.JobTerminated ? 0 : 70;
    }

    private static int Export()
    {
        EnsureSafeDirectory(ResultRoot, false);
        EnsureSafeDirectory(ExportRoot, false);
        EnsureAbsent(ExportPath);
        byte[] resultBytes = ReadSafeFile(ResultPath, MaxResultBytes);
        GuestResult result = GuestResult.Parse(resultBytes);
        byte[] markerBytes = ReadSafeFile(MarkerPath, 1024);
        string marker = new UTF8Encoding(false, true).GetString(markerBytes);
        string expectedMarker = result.RunId + "\n"
            + result.RequestHash + "\n"
            + result.InstanceId + "\n"
            + result.ResultHash + "\n";
        if (!String.Equals(marker, expectedMarker, StringComparison.Ordinal))
        {
            return 65;
        }
        WriteAtomic(ExportRoot, ExportPath, resultBytes);
        return 0;
    }

    private static GuestResult RunChild(GuestRequest request)
    {
        string executable = ResolveSnapshotPath(request.Executable, true);
        string workingDirectory = ResolveSnapshotPath(request.Cwd, false);
        if (!Directory.Exists(workingDirectory))
        {
            return GuestResult.Failure(
                request,
                "runner_error",
                "runner_failure",
                null,
                new byte[0],
                new byte[0],
                0,
                true);
        }

        Stopwatch stopwatch = Stopwatch.StartNew();
        IntPtr stdoutRead = IntPtr.Zero;
        IntPtr stdoutWrite = IntPtr.Zero;
        IntPtr stderrRead = IntPtr.Zero;
        IntPtr stderrWrite = IntPtr.Zero;
        IntPtr nullInput = IntPtr.Zero;
        IntPtr job = IntPtr.Zero;
        PROCESS_INFORMATION processInformation = new PROCESS_INFORMATION();
        OutputReader stdoutReader = null;
        OutputReader stderrReader = null;
        SharedOutputBudget budget = new SharedOutputBudget(request.MaxOutputBytes);
        string status = "runner_error";
        string errorCode = "runner_failure";
        int? exitCode = null;
        bool processCreated = false;

        try
        {
            SECURITY_ATTRIBUTES security = new SECURITY_ATTRIBUTES();
            security.nLength = Marshal.SizeOf(typeof(SECURITY_ATTRIBUTES));
            security.bInheritHandle = true;
            if (!CreatePipe(out stdoutRead, out stdoutWrite, ref security, 0)
                || !CreatePipe(out stderrRead, out stderrWrite, ref security, 0))
            {
                return GuestResult.Failure(
                    request,
                    "runner_error",
                    "runner_failure",
                    null,
                    new byte[0],
                    new byte[0],
                    ElapsedMilliseconds(stopwatch),
                    true);
            }
            if (!SetHandleInformation(stdoutRead, HandleFlagInherit, 0)
                || !SetHandleInformation(stderrRead, HandleFlagInherit, 0))
            {
                return GuestResult.Failure(
                    request,
                    "runner_error",
                    "runner_failure",
                    null,
                    new byte[0],
                    new byte[0],
                    ElapsedMilliseconds(stopwatch),
                    true);
            }
            nullInput = CreateFile(
                "NUL",
                GenericRead,
                0,
                ref security,
                OpenExisting,
                FileAttributeNormal,
                IntPtr.Zero);
            if (nullInput == InvalidHandleValue)
            {
                nullInput = IntPtr.Zero;
                return GuestResult.Failure(
                    request,
                    "runner_error",
                    "runner_failure",
                    null,
                    new byte[0],
                    new byte[0],
                    ElapsedMilliseconds(stopwatch),
                    true);
            }

            STARTUPINFOEX startup = new STARTUPINFOEX();
            startup.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
            startup.StartupInfo.dwFlags = StartfUseStdHandles;
            startup.StartupInfo.hStdInput = nullInput;
            startup.StartupInfo.hStdOutput = stdoutWrite;
            startup.StartupInfo.hStdError = stderrWrite;
            string commandLine = BuildCommandLine(executable, request.Arguments);
            IntPtr environment = BuildEnvironmentBlock(request.Environment);
            bool created;
            try
            {
                using (ProcessHandleList inherited = new ProcessHandleList(
                    new IntPtr[] { nullInput, stdoutWrite, stderrWrite }))
                {
                    startup.lpAttributeList = inherited.AttributeList;
                    created = CreateProcess(
                        executable,
                        new StringBuilder(commandLine),
                        IntPtr.Zero,
                        IntPtr.Zero,
                        true,
                        CreateSuspended
                            | CreateNoWindow
                            | CreateUnicodeEnvironment
                            | ExtendedStartupInfoPresent,
                        environment,
                        workingDirectory,
                        ref startup,
                        out processInformation);
                }
            }
            finally
            {
                if (environment != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(environment);
                }
            }
            CloseHandleIfValid(ref stdoutWrite);
            CloseHandleIfValid(ref stderrWrite);
            CloseHandleIfValid(ref nullInput);
            if (!created)
            {
                return GuestResult.Failure(
                    request,
                    "runner_error",
                    "create_process",
                    null,
                    new byte[0],
                    new byte[0],
                    ElapsedMilliseconds(stopwatch),
                    true);
            }
            processCreated = true;

            job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero
                || !ConfigureJob(job, request)
                || !AssignProcessToJobObject(job, processInformation.hProcess))
            {
                TerminateProcess(processInformation.hProcess, 1);
                bool suspendedProcessTerminated =
                    WaitForSingleObject(processInformation.hProcess, 5000) == WaitObject0;
                return GuestResult.Failure(
                    request,
                    "runner_error",
                    "job_setup",
                    null,
                    new byte[0],
                    new byte[0],
                    ElapsedMilliseconds(stopwatch),
                    suspendedProcessTerminated);
            }

            stdoutReader = new OutputReader(stdoutRead, budget);
            stderrReader = new OutputReader(stderrRead, budget);
            stdoutRead = IntPtr.Zero;
            stderrRead = IntPtr.Zero;
            stdoutReader.Start();
            stderrReader.Start();
            if (ResumeThread(processInformation.hThread) == UInt32.MaxValue)
            {
                TerminateJobObject(job, 1);
                return GuestResult.Failure(
                    request,
                    "runner_error",
                    "runner_failure",
                    null,
                    new byte[0],
                    new byte[0],
                    ElapsedMilliseconds(stopwatch),
                    TerminateAndConfirmEmptyJob(job, 5000));
            }

            status = "exited";
            errorCode = null;
            while (true)
            {
                uint wait = WaitForSingleObject(processInformation.hProcess, 20);
                if (wait == WaitObject0)
                {
                    break;
                }
                if (wait != WaitTimeout)
                {
                    status = "runner_error";
                    errorCode = "runner_failure";
                    TerminateJobObject(job, 1);
                    break;
                }
                if (budget.Exceeded)
                {
                    status = "output_limit";
                    errorCode = "output_limit";
                    TerminateJobObject(job, 1);
                    break;
                }
                if (SafeCancellationRequested())
                {
                    status = "cancelled";
                    errorCode = "cancelled";
                    TerminateJobObject(job, 1);
                    break;
                }
                if (stopwatch.ElapsedMilliseconds >= request.TimeoutMs)
                {
                    status = "timeout";
                    errorCode = "timeout";
                    TerminateJobObject(job, 1);
                    break;
                }
            }

            WaitForSingleObject(processInformation.hProcess, 5000);
            uint rawExitCode;
            if (GetExitCodeProcess(processInformation.hProcess, out rawExitCode))
            {
                exitCode = unchecked((int)rawExitCode);
                if (status == "exited" && IsResourceExit(rawExitCode))
                {
                    status = "resource_limit";
                    errorCode = "resource_limit";
                }
            }
            else if (status == "exited")
            {
                status = "runner_error";
                errorCode = "runner_failure";
            }

            // Closing or terminating the complete job is mandatory even when the
            // direct child exited normally; descendants must not outlive the result.
            bool jobTerminated = TerminateAndConfirmEmptyJob(job, 5000);
            if (!jobTerminated)
            {
                status = "runner_error";
                errorCode = "runner_failure";
            }
            if (stdoutReader != null && !stdoutReader.Join(5000))
            {
                stdoutReader.Stop();
                status = "runner_error";
                errorCode = "runner_failure";
            }
            if (stderrReader != null && !stderrReader.Join(5000))
            {
                stderrReader.Stop();
                status = "runner_error";
                errorCode = "runner_failure";
            }
            // A short-lived process can fill the anonymous pipe and exit before
            // the 20 ms process wait observes SharedOutputBudget.Exceeded.  The
            // readers are authoritative once both pipes have reached EOF, so a
            // successful direct-child exit must be reclassified after joining.
            if (budget.Exceeded
                && String.Equals(status, "exited", StringComparison.Ordinal))
            {
                status = "output_limit";
                errorCode = "output_limit";
            }

            byte[] stdout = stdoutReader == null
                ? new byte[0]
                : stdoutReader.GetBytes();
            byte[] stderr = stderrReader == null
                ? new byte[0]
                : stderrReader.GetBytes();
            if ((stdoutReader != null && stdoutReader.Failed)
                || (stderrReader != null && stderrReader.Failed))
            {
                status = "runner_error";
                errorCode = "runner_failure";
            }
            if (status == "exited")
            {
                return GuestResult.Exited(
                    request,
                    exitCode.GetValueOrDefault(),
                    stdout,
                    stderr,
                    ElapsedMilliseconds(stopwatch),
                    jobTerminated);
            }
            return GuestResult.Failure(
                request,
                status,
                errorCode,
                exitCode,
                stdout,
                stderr,
                ElapsedMilliseconds(stopwatch),
                jobTerminated);
        }
        finally
        {
            if (processCreated && job != IntPtr.Zero)
            {
                TerminateJobObject(job, 1);
            }
            if (stdoutReader != null)
            {
                stdoutReader.Stop();
            }
            if (stderrReader != null)
            {
                stderrReader.Stop();
            }
            CloseHandleIfValid(ref stdoutRead);
            CloseHandleIfValid(ref stdoutWrite);
            CloseHandleIfValid(ref stderrRead);
            CloseHandleIfValid(ref stderrWrite);
            CloseHandleIfValid(ref nullInput);
            CloseHandleIfValid(ref processInformation.hThread);
            CloseHandleIfValid(ref processInformation.hProcess);
            CloseHandleIfValid(ref job);
        }
    }

    private static bool ConfigureJob(IntPtr job, GuestRequest request)
    {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits =
            new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        limits.BasicLimitInformation.LimitFlags =
            JobObjectLimitKillOnJobClose
            | JobObjectLimitActiveProcess
            | JobObjectLimitProcessMemory
            | JobObjectLimitJobMemory;
        limits.BasicLimitInformation.ActiveProcessLimit =
            unchecked((uint)request.ActiveProcessLimit);
        limits.ProcessMemoryLimit =
            new UIntPtr(unchecked((ulong)request.ProcessMemoryBytes));
        limits.JobMemoryLimit =
            new UIntPtr(unchecked((ulong)request.JobMemoryBytes));
        int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr pointer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(limits, pointer, false);
            return SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                pointer,
                unchecked((uint)size));
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
    }

    private static bool TerminateAndConfirmEmptyJob(IntPtr job, int timeoutMs)
    {
        if (job == IntPtr.Zero)
        {
            return false;
        }
        uint activeProcesses;
        if (TryGetActiveJobProcesses(job, out activeProcesses) && activeProcesses == 0)
        {
            return true;
        }
        if (!TerminateJobObject(job, 1))
        {
            return false;
        }
        Stopwatch wait = Stopwatch.StartNew();
        while (wait.ElapsedMilliseconds <= timeoutMs)
        {
            if (TryGetActiveJobProcesses(job, out activeProcesses)
                && activeProcesses == 0)
            {
                return true;
            }
            Thread.Sleep(10);
        }
        return false;
    }

    private static bool TryGetActiveJobProcesses(
        IntPtr job,
        out uint activeProcesses)
    {
        JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting =
            new JOBOBJECT_BASIC_ACCOUNTING_INFORMATION();
        int size = Marshal.SizeOf(typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
        IntPtr pointer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(accounting, pointer, false);
            uint returnedLength;
            if (!QueryInformationJobObject(
                job,
                1,
                pointer,
                unchecked((uint)size),
                out returnedLength))
            {
                activeProcesses = UInt32.MaxValue;
                return false;
            }
            accounting = (JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                Marshal.PtrToStructure(
                    pointer,
                    typeof(JOBOBJECT_BASIC_ACCOUNTING_INFORMATION));
            activeProcesses = accounting.ActiveProcesses;
            return returnedLength == 0 || returnedLength >= size;
        }
        finally
        {
            Marshal.FreeHGlobal(pointer);
        }
    }

    private static string ResolveSnapshotPath(string relative, bool requireFile)
    {
        EnsureSafeDirectory(SnapshotRoot, false);
        string root = Path.GetFullPath(SnapshotRoot);
        string combined = relative == "."
            ? root
            : Path.GetFullPath(Path.Combine(root, relative));
        string prefix = root.TrimEnd('\\') + "\\";
        if (!String.Equals(combined, root, StringComparison.OrdinalIgnoreCase)
            && !combined.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("snapshot path escaped");
        }
        EnsureNoReparseComponents(root, combined);
        if (requireFile)
        {
            EnsureSafeFile(combined);
        }
        else
        {
            EnsureSafeDirectory(combined, false);
        }
        return combined;
    }

    private static void EnsureNoReparseComponents(string root, string target)
    {
        string relative = target.Substring(root.Length).TrimStart('\\');
        string current = root;
        if (relative.Length == 0)
        {
            return;
        }
        string[] parts = relative.Split('\\');
        for (int index = 0; index < parts.Length; index++)
        {
            current = Path.Combine(current, parts[index]);
            FileAttributes attributes = File.GetAttributes(current);
            if ((attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidDataException("reparse point rejected");
            }
        }
    }

    private static bool SafeCancellationRequested()
    {
        if (!File.Exists(CancelPath))
        {
            return false;
        }
        EnsureSafeFile(CancelPath);
        FileInfo info = new FileInfo(CancelPath);
        if (info.Length > 64)
        {
            throw new InvalidDataException("cancel signal too large");
        }
        return true;
    }

    private static IntPtr BuildEnvironmentBlock(
        SortedDictionary<string, string> requested)
    {
        SortedDictionary<string, string> values =
            new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        values["SystemRoot"] = @"C:\Windows";
        values["WINDIR"] = @"C:\Windows";
        values["TEMP"] = ScratchRoot;
        values["TMP"] = ScratchRoot;
        values["USERPROFILE"] = ScratchRoot;
        values["HOME"] = ScratchRoot;
        foreach (KeyValuePair<string, string> pair in requested)
        {
            values[pair.Key] = pair.Value;
        }
        StringBuilder block = new StringBuilder();
        foreach (KeyValuePair<string, string> pair in values)
        {
            block.Append(pair.Key);
            block.Append('=');
            block.Append(pair.Value);
            block.Append('\0');
        }
        block.Append('\0');
        return Marshal.StringToHGlobalUni(block.ToString());
    }

    private static string BuildCommandLine(
        string executable,
        IList<string> arguments)
    {
        StringBuilder command = new StringBuilder();
        command.Append(QuoteWindowsArgument(executable));
        for (int index = 0; index < arguments.Count; index++)
        {
            command.Append(' ');
            command.Append(QuoteWindowsArgument(arguments[index]));
        }
        return command.ToString();
    }

    private static string QuoteWindowsArgument(string argument)
    {
        if (argument.Length > 0
            && argument.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '"' }) < 0)
        {
            return argument;
        }
        StringBuilder quoted = new StringBuilder();
        quoted.Append('"');
        int backslashes = 0;
        for (int index = 0; index < argument.Length; index++)
        {
            char character = argument[index];
            if (character == '\\')
            {
                backslashes++;
                continue;
            }
            if (character == '"')
            {
                quoted.Append('\\', (backslashes * 2) + 1);
                quoted.Append('"');
                backslashes = 0;
                continue;
            }
            quoted.Append('\\', backslashes);
            backslashes = 0;
            quoted.Append(character);
        }
        quoted.Append('\\', backslashes * 2);
        quoted.Append('"');
        return quoted.ToString();
    }

    private static int ElapsedMilliseconds(Stopwatch stopwatch)
    {
        return unchecked((int)Math.Min(stopwatch.ElapsedMilliseconds, Int32.MaxValue));
    }

    private static bool IsResourceExit(uint exitCode)
    {
        return exitCode == 0xC0000017
            || exitCode == 0xC0000044
            || exitCode == 0xC000012D;
    }

    private static void EnsureSafeDirectory(string path, bool create)
    {
        if (create)
        {
            Directory.CreateDirectory(path);
        }
        DirectoryInfo info = new DirectoryInfo(path);
        if (!info.Exists || (info.Attributes & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidDataException("unsafe directory");
        }
    }

    private static void EnsureSafeFile(string path)
    {
        FileInfo info = new FileInfo(path);
        if (!info.Exists
            || (info.Attributes & FileAttributes.Directory) != 0
            || (info.Attributes & FileAttributes.ReparsePoint) != 0)
        {
            throw new InvalidDataException("unsafe file");
        }
    }

    private static void EnsureAbsent(string path)
    {
        if (File.Exists(path) || Directory.Exists(path))
        {
            throw new IOException("fixed output already exists");
        }
    }

    private static byte[] ReadSafeFile(string path, int maximum)
    {
        EnsureSafeFile(path);
        FileInfo before = new FileInfo(path);
        if (before.Length <= 0 || before.Length > maximum)
        {
            throw new InvalidDataException("file size rejected");
        }
        using (FileStream stream = new FileStream(
            path,
            FileMode.Open,
            FileAccess.Read,
            FileShare.Read,
            4096,
            FileOptions.SequentialScan))
        {
            if (stream.Length != before.Length || stream.Length > maximum)
            {
                throw new InvalidDataException("file changed while opening");
            }
            byte[] data = new byte[stream.Length];
            int offset = 0;
            while (offset < data.Length)
            {
                int read = stream.Read(data, offset, data.Length - offset);
                if (read == 0)
                {
                    throw new EndOfStreamException();
                }
                offset += read;
            }
            return data;
        }
    }

    private static void WriteAtomic(string directory, string destination, byte[] data)
    {
        EnsureSafeDirectory(directory, false);
        EnsureAbsent(destination);
        string temporary = Path.Combine(
            directory,
            "." + Path.GetFileName(destination) + "." + Guid.NewGuid().ToString("N"));
        try
        {
            using (FileStream stream = new FileStream(
                temporary,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None,
                4096,
                FileOptions.WriteThrough))
            {
                stream.Write(data, 0, data.Length);
                stream.Flush(true);
            }
            EnsureSafeFile(temporary);
            if (!MoveFileEx(temporary, destination, MoveFileWriteThrough))
            {
                int error = Marshal.GetLastWin32Error();
                if (error == ErrorAlreadyExists)
                {
                    throw new IOException("fixed output already exists");
                }
                throw new IOException("atomic move failed");
            }
        }
        finally
        {
            try
            {
                if (File.Exists(temporary))
                {
                    File.Delete(temporary);
                }
            }
            catch
            {
                // The operation already fails closed; cleanup is best effort.
            }
        }
    }

    private static void CloseHandleIfValid(ref IntPtr handle)
    {
        if (handle != IntPtr.Zero && handle != InvalidHandleValue)
        {
            CloseHandle(handle);
        }
        handle = IntPtr.Zero;
    }

    private sealed class SharedOutputBudget
    {
        private readonly object sync = new object();
        private readonly int maximum;
        private int used;
        private bool exceeded;

        internal SharedOutputBudget(int maximum)
        {
            this.maximum = maximum;
        }

        internal bool Exceeded
        {
            get
            {
                lock (sync)
                {
                    return exceeded;
                }
            }
        }

        internal int Reserve(int requested)
        {
            lock (sync)
            {
                int remaining = maximum - used;
                int accepted = Math.Min(Math.Max(remaining, 0), requested);
                used += accepted;
                if (accepted != requested)
                {
                    exceeded = true;
                }
                return accepted;
            }
        }
    }

    private sealed class ProcessHandleList : IDisposable
    {
        private IntPtr attributeList;
        private IntPtr handles;
        private bool initialized;

        internal ProcessHandleList(IntPtr[] allowedHandles)
        {
            if (allowedHandles == null || allowedHandles.Length == 0)
            {
                throw new ArgumentException("allowed handle list is empty");
            }
            UIntPtr size = UIntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref size);
            ulong sizeValue = size.ToUInt64();
            if (sizeValue == 0 || sizeValue > Int32.MaxValue)
            {
                throw new InvalidOperationException("attribute list size rejected");
            }
            attributeList = Marshal.AllocHGlobal(unchecked((int)sizeValue));
            if (!InitializeProcThreadAttributeList(
                attributeList,
                1,
                0,
                ref size))
            {
                Marshal.FreeHGlobal(attributeList);
                attributeList = IntPtr.Zero;
                throw new InvalidOperationException("attribute list initialization failed");
            }
            initialized = true;
            handles = Marshal.AllocHGlobal(IntPtr.Size * allowedHandles.Length);
            for (int index = 0; index < allowedHandles.Length; index++)
            {
                Marshal.WriteIntPtr(handles, index * IntPtr.Size, allowedHandles[index]);
            }
            UIntPtr handlesSize = new UIntPtr(
                unchecked((uint)(IntPtr.Size * allowedHandles.Length)));
            if (!UpdateProcThreadAttribute(
                attributeList,
                0,
                new IntPtr(ProcThreadAttributeHandleList),
                handles,
                handlesSize,
                IntPtr.Zero,
                IntPtr.Zero))
            {
                Dispose();
                throw new InvalidOperationException("handle allowlist setup failed");
            }
        }

        internal IntPtr AttributeList
        {
            get { return attributeList; }
        }

        public void Dispose()
        {
            if (attributeList != IntPtr.Zero)
            {
                if (initialized)
                {
                    DeleteProcThreadAttributeList(attributeList);
                }
                Marshal.FreeHGlobal(attributeList);
                attributeList = IntPtr.Zero;
                initialized = false;
            }
            if (handles != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(handles);
                handles = IntPtr.Zero;
            }
        }
    }

    private sealed class OutputReader
    {
        private readonly FileStream stream;
        private readonly SharedOutputBudget budget;
        private readonly MemoryStream output;
        private readonly Thread thread;
        private volatile bool failed;

        internal OutputReader(IntPtr handle, SharedOutputBudget budget)
        {
            this.stream = new FileStream(
                new SafeFileHandle(handle, true),
                FileAccess.Read,
                4096,
                false);
            this.budget = budget;
            this.output = new MemoryStream();
            this.thread = new Thread(Read);
            this.thread.IsBackground = true;
        }

        internal bool Failed
        {
            get { return failed; }
        }

        internal void Start()
        {
            thread.Start();
        }

        internal bool Join(int milliseconds)
        {
            return thread.Join(milliseconds);
        }

        internal void Stop()
        {
            try
            {
                stream.Dispose();
            }
            catch
            {
                failed = true;
            }
        }

        internal byte[] GetBytes()
        {
            lock (output)
            {
                return output.ToArray();
            }
        }

        private void Read()
        {
            byte[] buffer = new byte[4096];
            try
            {
                while (true)
                {
                    int count = stream.Read(buffer, 0, buffer.Length);
                    if (count == 0)
                    {
                        return;
                    }
                    int accepted = budget.Reserve(count);
                    if (accepted > 0)
                    {
                        lock (output)
                        {
                            output.Write(buffer, 0, accepted);
                        }
                    }
                    if (accepted != count)
                    {
                        return;
                    }
                }
            }
            catch (ObjectDisposedException)
            {
                return;
            }
            catch
            {
                failed = true;
            }
            finally
            {
                try
                {
                    stream.Dispose();
                }
                catch
                {
                    failed = true;
                }
            }
        }
    }

    private sealed class GuestRequest
    {
        internal string RunId;
        internal string RequestHash;
        internal string InstanceId;
        internal string Executable;
        internal List<string> Arguments;
        internal string Cwd;
        internal SortedDictionary<string, string> Environment;
        internal int TimeoutMs;
        internal int MaxOutputBytes;
        internal int ActiveProcessLimit;
        internal long ProcessMemoryBytes;
        internal long JobMemoryBytes;

        internal static GuestRequest Parse(byte[] data)
        {
            UTF8Encoding encoding = new UTF8Encoding(false, true);
            string raw = encoding.GetString(data);
            JavaScriptSerializer serializer = new JavaScriptSerializer();
            serializer.MaxJsonLength = MaxRequestBytes;
            object parsed = serializer.DeserializeObject(raw);
            Dictionary<string, object> root = RequireDictionary(parsed);
            RequireExactKeys(
                root,
                new string[] {
                    "version",
                    "run_id",
                    "request_hash",
                    "instance_id",
                    "executable",
                    "argv",
                    "cwd",
                    "environment",
                    "timeout_ms",
                    "max_output_bytes",
                    "active_process_limit",
                    "process_memory_bytes",
                    "job_memory_bytes"
                });
            if (!String.Equals(raw, CanonicalJson.Serialize(root), StringComparison.Ordinal))
            {
                throw new InvalidDataException("request is not canonical");
            }
            if (RequireInt(root["version"]) != ProtocolVersion)
            {
                throw new InvalidDataException("unsupported protocol");
            }

            GuestRequest request = new GuestRequest();
            request.RunId = RequireHex(root["run_id"], 32);
            request.RequestHash = RequireHex(root["request_hash"], 64);
            request.InstanceId = RequireHex(root["instance_id"], 32);
            request.Executable = RequireString(root["executable"]);
            ValidateRelativePath(request.Executable, false);
            if (!request.Executable.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException("executable suffix rejected");
            }
            request.Cwd = RequireString(root["cwd"]);
            ValidateRelativePath(request.Cwd, true);
            request.Arguments = ParseArguments(root["argv"]);
            request.Environment = ParseEnvironment(root["environment"]);
            request.TimeoutMs = RequireRange(
                root["timeout_ms"],
                MinTimeoutMs,
                MaxTimeoutMs);
            request.MaxOutputBytes = RequireRange(
                root["max_output_bytes"],
                MinOutputBytes,
                SandboxGuestRunner.MaxOutputBytes);
            request.ActiveProcessLimit = RequireRange(
                root["active_process_limit"],
                1,
                MaxActiveProcesses);
            request.ProcessMemoryBytes = RequireLongRange(
                root["process_memory_bytes"],
                MinMemoryBytes,
                MaxProcessMemoryBytes);
            request.JobMemoryBytes = RequireLongRange(
                root["job_memory_bytes"],
                MinMemoryBytes,
                MaxJobMemoryBytes);
            if (request.JobMemoryBytes < request.ProcessMemoryBytes)
            {
                throw new InvalidDataException("memory limits are inconsistent");
            }
            Dictionary<string, object> hashPayload =
                new Dictionary<string, object>(root, StringComparer.Ordinal);
            hashPayload.Remove("request_hash");
            if (!String.Equals(
                request.RequestHash,
                CanonicalJson.Digest(hashPayload),
                StringComparison.Ordinal))
            {
                throw new InvalidDataException("request hash mismatch");
            }
            return request;
        }

        private static List<string> ParseArguments(object value)
        {
            object[] values = value as object[];
            if (values == null || values.Length > MaxArguments)
            {
                throw new InvalidDataException("arguments rejected");
            }
            List<string> arguments = new List<string>();
            for (int index = 0; index < values.Length; index++)
            {
                string argument = RequireString(values[index]);
                ValidateText(argument, MaxArgumentChars);
                arguments.Add(argument);
            }
            return arguments;
        }

        private static SortedDictionary<string, string> ParseEnvironment(object value)
        {
            Dictionary<string, object> values = RequireDictionary(value);
            if (values.Count > MaxEnvironmentItems)
            {
                throw new InvalidDataException("environment rejected");
            }
            SortedDictionary<string, string> environment =
                new SortedDictionary<string, string>(StringComparer.Ordinal);
            foreach (KeyValuePair<string, object> pair in values)
            {
                if (!IsEnvironmentName(pair.Key))
                {
                    throw new InvalidDataException("environment name rejected");
                }
                string item = RequireString(pair.Value);
                ValidateText(item, MaxEnvironmentValueChars);
                environment.Add(pair.Key, item);
            }
            return environment;
        }
    }

    private sealed class GuestResult
    {
        internal string RunId;
        internal string RequestHash;
        internal string InstanceId;
        internal string Status;
        internal int? ExitCode;
        internal byte[] Stdout;
        internal byte[] Stderr;
        internal int DurationMs;
        internal string ErrorCode;
        internal bool JobTerminated;
        internal string ResultHash;

        internal static GuestResult Exited(
            GuestRequest request,
            int exitCode,
            byte[] stdout,
            byte[] stderr,
            int durationMs,
            bool jobTerminated)
        {
            return Create(
                request,
                "exited",
                null,
                exitCode,
                stdout,
                stderr,
                durationMs,
                jobTerminated);
        }

        internal static GuestResult Failure(
            GuestRequest request,
            string status,
            string errorCode,
            int? exitCode,
            byte[] stdout,
            byte[] stderr,
            int durationMs,
            bool jobTerminated)
        {
            return Create(
                request,
                status,
                errorCode,
                exitCode,
                stdout,
                stderr,
                durationMs,
                jobTerminated);
        }

        private static GuestResult Create(
            GuestRequest request,
            string status,
            string errorCode,
            int? exitCode,
            byte[] stdout,
            byte[] stderr,
            int durationMs,
            bool jobTerminated)
        {
            GuestResult result = new GuestResult();
            result.RunId = request.RunId;
            result.RequestHash = request.RequestHash;
            result.InstanceId = request.InstanceId;
            result.Status = status;
            result.ExitCode = exitCode;
            result.Stdout = stdout;
            result.Stderr = stderr;
            result.DurationMs = durationMs;
            result.ErrorCode = errorCode;
            result.JobTerminated = jobTerminated;
            result.ResultHash = CanonicalJson.Digest(result.HashPayload());
            return result;
        }

        internal byte[] CanonicalBytes()
        {
            return new UTF8Encoding(false, true).GetBytes(
                CanonicalJson.Serialize(FullPayload()));
        }

        internal static GuestResult Parse(byte[] data)
        {
            UTF8Encoding encoding = new UTF8Encoding(false, true);
            string raw = encoding.GetString(data);
            JavaScriptSerializer serializer = new JavaScriptSerializer();
            serializer.MaxJsonLength = MaxResultBytes;
            Dictionary<string, object> root =
                RequireDictionary(serializer.DeserializeObject(raw));
            RequireExactKeys(
                root,
                new string[] {
                    "version",
                    "runner_version",
                    "security_assurance",
                    "run_id",
                    "request_hash",
                    "instance_id",
                    "status",
                    "exit_code",
                    "stdout_b64",
                    "stderr_b64",
                    "stdout_bytes",
                    "stderr_bytes",
                    "duration_ms",
                    "error_code",
                    "job_terminated",
                    "result_hash"
                });
            if (!String.Equals(raw, CanonicalJson.Serialize(root), StringComparison.Ordinal))
            {
                throw new InvalidDataException("result is not canonical");
            }
            if (RequireInt(root["version"]) != ProtocolVersion
                || RequireInt(root["runner_version"]) != RunnerVersion
                || !String.Equals(
                    RequireString(root["security_assurance"]),
                    SecurityAssurance,
                    StringComparison.Ordinal))
            {
                throw new InvalidDataException("result version rejected");
            }
            GuestResult result = new GuestResult();
            result.RunId = RequireHex(root["run_id"], 32);
            result.RequestHash = RequireHex(root["request_hash"], 64);
            result.InstanceId = RequireHex(root["instance_id"], 32);
            result.Status = RequireString(root["status"]);
            result.ExitCode = root["exit_code"] == null
                ? (int?)null
                : RequireInt(root["exit_code"]);
            string stdoutBase64 = RequireString(root["stdout_b64"]);
            string stderrBase64 = RequireString(root["stderr_b64"]);
            result.Stdout = Convert.FromBase64String(stdoutBase64);
            result.Stderr = Convert.FromBase64String(stderrBase64);
            if (!String.Equals(
                    Convert.ToBase64String(result.Stdout),
                    stdoutBase64,
                    StringComparison.Ordinal)
                || !String.Equals(
                    Convert.ToBase64String(result.Stderr),
                    stderrBase64,
                    StringComparison.Ordinal))
            {
                throw new InvalidDataException("result base64 is not canonical");
            }
            if (RequireInt(root["stdout_bytes"]) != result.Stdout.Length
                || RequireInt(root["stderr_bytes"]) != result.Stderr.Length
                || result.Stdout.Length + result.Stderr.Length > MaxOutputBytes)
            {
                throw new InvalidDataException("result output rejected");
            }
            result.DurationMs = RequireRange(
                root["duration_ms"],
                0,
                MaxTimeoutMs + 10000);
            result.ErrorCode = root["error_code"] == null
                ? null
                : RequireString(root["error_code"]);
            ValidateResultState(result);
            result.JobTerminated = RequireBool(root["job_terminated"]);
            if (!result.JobTerminated)
            {
                throw new InvalidDataException("job termination was not confirmed");
            }
            result.ResultHash = RequireHex(root["result_hash"], 64);
            if (!String.Equals(
                result.ResultHash,
                CanonicalJson.Digest(result.HashPayload()),
                StringComparison.Ordinal))
            {
                throw new InvalidDataException("result hash mismatch");
            }
            return result;
        }

        private static void ValidateResultState(GuestResult result)
        {
            if (String.Equals(result.Status, "exited", StringComparison.Ordinal))
            {
                if (!result.ExitCode.HasValue || result.ErrorCode != null)
                {
                    throw new InvalidDataException("exited result state rejected");
                }
                return;
            }
            if (String.Equals(result.Status, "timeout", StringComparison.Ordinal)
                || String.Equals(
                    result.Status,
                    "cancelled",
                    StringComparison.Ordinal)
                || String.Equals(
                    result.Status,
                    "output_limit",
                    StringComparison.Ordinal)
                || String.Equals(
                    result.Status,
                    "resource_limit",
                    StringComparison.Ordinal))
            {
                if (!String.Equals(
                    result.ErrorCode,
                    result.Status,
                    StringComparison.Ordinal))
                {
                    throw new InvalidDataException("failure result state rejected");
                }
                return;
            }
            if (!String.Equals(
                    result.Status,
                    "runner_error",
                    StringComparison.Ordinal)
                || (!String.Equals(
                        result.ErrorCode,
                        "create_process",
                        StringComparison.Ordinal)
                    && !String.Equals(
                        result.ErrorCode,
                        "job_setup",
                        StringComparison.Ordinal)
                    && !String.Equals(
                        result.ErrorCode,
                        "runner_failure",
                        StringComparison.Ordinal)))
            {
                throw new InvalidDataException("runner result state rejected");
            }
        }

        internal Dictionary<string, object> HashPayload()
        {
            Dictionary<string, object> payload = FullPayload();
            payload.Remove("result_hash");
            return payload;
        }

        private Dictionary<string, object> FullPayload()
        {
            Dictionary<string, object> payload =
                new Dictionary<string, object>(StringComparer.Ordinal);
            payload["version"] = ProtocolVersion;
            payload["runner_version"] = RunnerVersion;
            payload["security_assurance"] = SecurityAssurance;
            payload["run_id"] = RunId;
            payload["request_hash"] = RequestHash;
            payload["instance_id"] = InstanceId;
            payload["status"] = Status;
            payload["exit_code"] = ExitCode.HasValue
                ? (object)ExitCode.Value
                : null;
            payload["stdout_b64"] = Convert.ToBase64String(Stdout);
            payload["stderr_b64"] = Convert.ToBase64String(Stderr);
            payload["stdout_bytes"] = Stdout.Length;
            payload["stderr_bytes"] = Stderr.Length;
            payload["duration_ms"] = DurationMs;
            payload["error_code"] = ErrorCode;
            payload["job_terminated"] = JobTerminated;
            payload["result_hash"] = ResultHash;
            return payload;
        }
    }

    private static Dictionary<string, object> RequireDictionary(object value)
    {
        Dictionary<string, object> dictionary =
            value as Dictionary<string, object>;
        if (dictionary == null)
        {
            throw new InvalidDataException("object required");
        }
        return dictionary;
    }

    private static void RequireExactKeys(
        Dictionary<string, object> dictionary,
        string[] expected)
    {
        if (dictionary.Count != expected.Length)
        {
            throw new InvalidDataException("field set rejected");
        }
        for (int index = 0; index < expected.Length; index++)
        {
            if (!dictionary.ContainsKey(expected[index]))
            {
                throw new InvalidDataException("field set rejected");
            }
        }
    }

    private static string RequireString(object value)
    {
        string text = value as string;
        if (text == null)
        {
            throw new InvalidDataException("string required");
        }
        return text;
    }

    private static int RequireInt(object value)
    {
        if (value is int)
        {
            return (int)value;
        }
        long longValue;
        if (value is long)
        {
            longValue = (long)value;
            if (longValue >= Int32.MinValue && longValue <= Int32.MaxValue)
            {
                return unchecked((int)longValue);
            }
        }
        throw new InvalidDataException("integer required");
    }

    private static bool RequireBool(object value)
    {
        if (!(value is bool))
        {
            throw new InvalidDataException("boolean required");
        }
        return (bool)value;
    }

    private static long RequireLong(object value)
    {
        if (value is int)
        {
            return (int)value;
        }
        if (value is long)
        {
            return (long)value;
        }
        throw new InvalidDataException("integer required");
    }

    private static int RequireRange(object value, int minimum, int maximum)
    {
        int number = RequireInt(value);
        if (number < minimum || number > maximum)
        {
            throw new InvalidDataException("integer range rejected");
        }
        return number;
    }

    private static long RequireLongRange(object value, long minimum, long maximum)
    {
        long number = RequireLong(value);
        if (number < minimum || number > maximum)
        {
            throw new InvalidDataException("integer range rejected");
        }
        return number;
    }

    private static string RequireHex(object value, int length)
    {
        string text = RequireString(value);
        if (text.Length != length)
        {
            throw new InvalidDataException("hex value rejected");
        }
        for (int index = 0; index < text.Length; index++)
        {
            char character = text[index];
            if (!((character >= '0' && character <= '9')
                || (character >= 'a' && character <= 'f')))
            {
                throw new InvalidDataException("hex value rejected");
            }
        }
        return text;
    }

    private static void ValidateRelativePath(string value, bool allowDot)
    {
        if (allowDot && String.Equals(value, ".", StringComparison.Ordinal))
        {
            return;
        }
        ValidateText(value, MaxRelativePathChars);
        if (value.Length == 0
            || value.IndexOf('/') >= 0
            || value.IndexOf(':') >= 0
            || value.StartsWith("\\", StringComparison.Ordinal)
            || value.EndsWith("\\", StringComparison.Ordinal))
        {
            throw new InvalidDataException("relative path rejected");
        }
        string[] parts = value.Split('\\');
        for (int index = 0; index < parts.Length; index++)
        {
            string part = parts[index];
            if (part.Length == 0
                || String.Equals(part, ".", StringComparison.Ordinal)
                || String.Equals(part, "..", StringComparison.Ordinal)
                || part.EndsWith(" ", StringComparison.Ordinal)
                || part.EndsWith(".", StringComparison.Ordinal)
                || IsReservedWindowsName(part))
            {
                throw new InvalidDataException("path component rejected");
            }
        }
    }

    private static bool IsReservedWindowsName(string part)
    {
        string stem = part.Split('.')[0].ToUpperInvariant();
        if (stem == "CON" || stem == "PRN" || stem == "AUX" || stem == "NUL")
        {
            return true;
        }
        if (stem.Length == 4
            && (stem.StartsWith("COM", StringComparison.Ordinal)
                || stem.StartsWith("LPT", StringComparison.Ordinal))
            && stem[3] >= '1'
            && stem[3] <= '9')
        {
            return true;
        }
        return false;
    }

    private static bool IsEnvironmentName(string value)
    {
        if (!value.StartsWith("NEIL_", StringComparison.Ordinal)
            || value.Length < 6
            || value.Length > 63)
        {
            return false;
        }
        for (int index = 5; index < value.Length; index++)
        {
            char character = value[index];
            if (!((character >= 'A' && character <= 'Z')
                || (character >= '0' && character <= '9')
                || character == '_'))
            {
                return false;
            }
        }
        return true;
    }

    private static void ValidateText(string value, int maximum)
    {
        if (value.Length > maximum)
        {
            throw new InvalidDataException("text length rejected");
        }
        for (int index = 0; index < value.Length; index++)
        {
            char character = value[index];
            if (Char.IsHighSurrogate(character))
            {
                if (index + 1 >= value.Length
                    || !Char.IsLowSurrogate(value[index + 1]))
                {
                    throw new InvalidDataException("unpaired surrogate rejected");
                }
                UnicodeCategory pairCategory =
                    CharUnicodeInfo.GetUnicodeCategory(value, index);
                if (pairCategory == UnicodeCategory.Control
                    || pairCategory == UnicodeCategory.Format)
                {
                    throw new InvalidDataException("control character rejected");
                }
                index++;
                continue;
            }
            if (Char.IsLowSurrogate(character))
            {
                throw new InvalidDataException("unpaired surrogate rejected");
            }
            UnicodeCategory textCategory =
                CharUnicodeInfo.GetUnicodeCategory(value, index);
            if (textCategory == UnicodeCategory.Control
                || textCategory == UnicodeCategory.Format)
            {
                throw new InvalidDataException("control character rejected");
            }
        }
    }

    private static class CanonicalJson
    {
        internal static string Serialize(object value)
        {
            StringBuilder builder = new StringBuilder();
            Append(builder, value);
            return builder.ToString();
        }

        internal static string Digest(object value)
        {
            byte[] bytes = new UTF8Encoding(false, true).GetBytes(Serialize(value));
            using (SHA256 algorithm = SHA256.Create())
            {
                byte[] digest = algorithm.ComputeHash(bytes);
                StringBuilder text = new StringBuilder(64);
                for (int index = 0; index < digest.Length; index++)
                {
                    text.Append(digest[index].ToString("x2", CultureInfo.InvariantCulture));
                }
                return text.ToString();
            }
        }

        private static void Append(StringBuilder builder, object value)
        {
            if (value == null)
            {
                builder.Append("null");
                return;
            }
            if (value is string)
            {
                AppendString(builder, (string)value);
                return;
            }
            if (value is bool)
            {
                builder.Append((bool)value ? "true" : "false");
                return;
            }
            if (value is int)
            {
                builder.Append(((int)value).ToString(CultureInfo.InvariantCulture));
                return;
            }
            if (value is long)
            {
                builder.Append(((long)value).ToString(CultureInfo.InvariantCulture));
                return;
            }
            IDictionary dictionary = value as IDictionary;
            if (dictionary != null)
            {
                List<string> keys = new List<string>();
                foreach (object key in dictionary.Keys)
                {
                    string text = key as string;
                    if (text == null)
                    {
                        throw new InvalidDataException("JSON key rejected");
                    }
                    keys.Add(text);
                }
                keys.Sort(StringComparer.Ordinal);
                builder.Append('{');
                for (int index = 0; index < keys.Count; index++)
                {
                    if (index > 0)
                    {
                        builder.Append(',');
                    }
                    AppendString(builder, keys[index]);
                    builder.Append(':');
                    Append(builder, dictionary[keys[index]]);
                }
                builder.Append('}');
                return;
            }
            IEnumerable sequence = value as IEnumerable;
            if (sequence != null)
            {
                builder.Append('[');
                bool first = true;
                foreach (object item in sequence)
                {
                    if (!first)
                    {
                        builder.Append(',');
                    }
                    first = false;
                    Append(builder, item);
                }
                builder.Append(']');
                return;
            }
            throw new InvalidDataException("JSON value rejected");
        }

        private static void AppendString(StringBuilder builder, string value)
        {
            builder.Append('"');
            for (int index = 0; index < value.Length; index++)
            {
                char character = value[index];
                switch (character)
                {
                    case '"':
                        builder.Append("\\\"");
                        break;
                    case '\\':
                        builder.Append("\\\\");
                        break;
                    case '\b':
                        builder.Append("\\b");
                        break;
                    case '\f':
                        builder.Append("\\f");
                        break;
                    case '\n':
                        builder.Append("\\n");
                        break;
                    case '\r':
                        builder.Append("\\r");
                        break;
                    case '\t':
                        builder.Append("\\t");
                        break;
                    default:
                        if (character < 0x20)
                        {
                            builder.Append("\\u");
                            builder.Append(
                                ((int)character).ToString(
                                    "x4",
                                    CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            builder.Append(character);
                        }
                        break;
                }
            }
            builder.Append('"');
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SECURITY_ATTRIBUTES
    {
        internal int nLength;
        internal IntPtr lpSecurityDescriptor;
        [MarshalAs(UnmanagedType.Bool)]
        internal bool bInheritHandle;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        internal int cb;
        internal string lpReserved;
        internal string lpDesktop;
        internal string lpTitle;
        internal uint dwX;
        internal uint dwY;
        internal uint dwXSize;
        internal uint dwYSize;
        internal uint dwXCountChars;
        internal uint dwYCountChars;
        internal uint dwFillAttribute;
        internal uint dwFlags;
        internal ushort wShowWindow;
        internal ushort cbReserved2;
        internal IntPtr lpReserved2;
        internal IntPtr hStdInput;
        internal IntPtr hStdOutput;
        internal IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        internal IntPtr hProcess;
        internal IntPtr hThread;
        internal uint dwProcessId;
        internal uint dwThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct STARTUPINFOEX
    {
        internal STARTUPINFO StartupInfo;
        internal IntPtr lpAttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        internal long PerProcessUserTimeLimit;
        internal long PerJobUserTimeLimit;
        internal uint LimitFlags;
        internal UIntPtr MinimumWorkingSetSize;
        internal UIntPtr MaximumWorkingSetSize;
        internal uint ActiveProcessLimit;
        internal UIntPtr Affinity;
        internal uint PriorityClass;
        internal uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        internal ulong ReadOperationCount;
        internal ulong WriteOperationCount;
        internal ulong OtherOperationCount;
        internal ulong ReadTransferCount;
        internal ulong WriteTransferCount;
        internal ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        internal JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        internal IO_COUNTERS IoInfo;
        internal UIntPtr ProcessMemoryLimit;
        internal UIntPtr JobMemoryLimit;
        internal UIntPtr PeakProcessMemoryUsed;
        internal UIntPtr PeakJobMemoryUsed;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    {
        internal long TotalUserTime;
        internal long TotalKernelTime;
        internal long ThisPeriodTotalUserTime;
        internal long ThisPeriodTotalKernelTime;
        internal uint TotalPageFaultCount;
        internal uint TotalProcesses;
        internal uint ActiveProcesses;
        internal uint TotalTerminatedProcesses;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreatePipe(
        out IntPtr hReadPipe,
        out IntPtr hWritePipe,
        ref SECURITY_ATTRIBUTES lpPipeAttributes,
        uint nSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetHandleInformation(
        IntPtr hObject,
        uint dwMask,
        uint dwFlags);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFile(
        string lpFileName,
        uint dwDesiredAccess,
        uint dwShareMode,
        ref SECURITY_ATTRIBUTES lpSecurityAttributes,
        uint dwCreationDisposition,
        uint dwFlagsAndAttributes,
        IntPtr hTemplateFile);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcess(
        string lpApplicationName,
        StringBuilder lpCommandLine,
        IntPtr lpProcessAttributes,
        IntPtr lpThreadAttributes,
        [MarshalAs(UnmanagedType.Bool)] bool bInheritHandles,
        uint dwCreationFlags,
        IntPtr lpEnvironment,
        string lpCurrentDirectory,
        ref STARTUPINFOEX lpStartupInfo,
        out PROCESS_INFORMATION lpProcessInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool InitializeProcThreadAttributeList(
        IntPtr lpAttributeList,
        int dwAttributeCount,
        int dwFlags,
        ref UIntPtr lpSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool UpdateProcThreadAttribute(
        IntPtr lpAttributeList,
        uint dwFlags,
        IntPtr attribute,
        IntPtr lpValue,
        UIntPtr cbSize,
        IntPtr lpPreviousValue,
        IntPtr lpReturnSize);

    [DllImport("kernel32.dll")]
    private static extern void DeleteProcThreadAttributeList(
        IntPtr lpAttributeList);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(
        IntPtr lpJobAttributes,
        string lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetInformationJobObject(
        IntPtr hJob,
        int JobObjectInfoClass,
        IntPtr lpJobObjectInfo,
        uint cbJobObjectInfoLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool QueryInformationJobObject(
        IntPtr hJob,
        int JobObjectInfoClass,
        IntPtr lpJobObjectInfo,
        uint cbJobObjectInfoLength,
        out uint lpReturnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool AssignProcessToJobObject(
        IntPtr hJob,
        IntPtr hProcess);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr hThread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetExitCodeProcess(
        IntPtr hProcess,
        out uint lpExitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool MoveFileEx(
        string lpExistingFileName,
        string lpNewFileName,
        uint dwFlags);
}
