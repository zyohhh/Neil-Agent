using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

internal static class SandboxSecurityProbe
{
    private const string GuestExportRoot = @"C:\NeilAgent\Export";
    private const string GuestScratchRoot = @"C:\NeilAgent\Scratch";
    private const string GuestResultRoot = @"C:\NeilAgent\Result";
    private const string GuestRequestPath = @"C:\NeilAgent\Control\request.json";
    private const uint ProcessCreateThread = 0x0002;
    private const uint ProcessVmOperation = 0x0008;
    private const uint ProcessVmWrite = 0x0020;
    private const uint ScManagerCreateService = 0x0002;
    private const uint CreateBreakawayFromJob = 0x01000000;
    private const uint CreateNoWindow = 0x08000000;
    private const int TokenIntegrityLevel = 25;

    private static string EffectiveScratchRoot
    {
        get
        {
            string configured = Environment.GetEnvironmentVariable(
                "NEIL_SCRATCH_ROOT");
            return String.IsNullOrEmpty(configured)
                ? GuestScratchRoot
                : configured;
        }
    }

    private static int Main(string[] args)
    {
        try
        {
            if (args.Length == 0)
            {
                return 64;
            }
            switch (args[0])
            {
                case "isolation":
                    return CheckIsolation(args);
                case "tree":
                    return StartTree();
                case "tree-child":
                    return StartTreeChild();
                case "tree-grandchild":
                case "sleep":
                    Thread.Sleep(120000);
                    return 0;
                case "flood":
                    return FloodOutput();
                case "memory":
                    return ExhaustMemory();
                case "process-limit":
                    return CheckProcessLimit();
                case "token-boundary":
                    return CheckTokenBoundary();
                case "broker-escape":
                    return CheckBrokerEscape();
                case "breakaway":
                    return CheckBreakaway();
                case "job-memory":
                    return CheckAggregateJobMemory();
                case "memory-hold":
                    return HoldMemory(args);
                default:
                    return 64;
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error.GetType().FullName);
            return 70;
        }
    }

    private static int CheckIsolation(string[] args)
    {
        if (args.Length < 3)
        {
            return 64;
        }
        List<string> failures = new List<string>();
        for (int index = 1; index < args.Length; index++)
        {
            if (CanRead(args[index]))
            {
                failures.Add("host-file-" + index.ToString(CultureInfo.InvariantCulture));
            }
        }

        string[] secretEnvironmentNames = {
            "DEEPSEEK_API_KEY",
            "GITHUB_TOKEN",
            "AWS_ACCESS_KEY_ID",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "PATH"
        };
        foreach (string name in secretEnvironmentNames)
        {
            if (!String.IsNullOrEmpty(Environment.GetEnvironmentVariable(name)))
            {
                failures.Add("environment-" + name);
            }
        }
        foreach (string name in new string[] { "HOME", "USERPROFILE", "TEMP", "TMP" })
        {
            string value = Environment.GetEnvironmentVariable(name);
            if (!String.Equals(
                value,
                EffectiveScratchRoot,
                StringComparison.OrdinalIgnoreCase))
            {
                failures.Add("scratch-" + name);
            }
        }

        string workspaceWrite = Path.Combine(
            Environment.CurrentDirectory,
            "sandbox-must-not-write.txt");
        try
        {
            File.WriteAllText(workspaceWrite, "unsafe", new UTF8Encoding(false));
            failures.Add("workspace-write");
        }
        catch (UnauthorizedAccessException)
        {
        }
        catch (IOException)
        {
        }

        try
        {
            Directory.CreateDirectory(GuestExportRoot);
            File.WriteAllText(
                Path.Combine(GuestExportRoot, "untrusted-before-share.txt"),
                "must remain inside the disposable guest",
                new UTF8Encoding(false));
        }
        catch (UnauthorizedAccessException)
        {
        }
        catch (IOException)
        {
        }

        if (CanConnect(IPAddress.Parse("1.1.1.1"), 443, 750))
        {
            failures.Add("ipv4");
        }
        if (CanConnect(IPAddress.Parse("2606:4700:4700::1111"), 443, 750))
        {
            failures.Add("ipv6");
        }
        if (CanResolve("example.com"))
        {
            failures.Add("dns");
        }

        if (failures.Count != 0)
        {
            Console.Error.WriteLine(String.Join(",", failures.ToArray()));
            return 1;
        }
        Console.WriteLine("isolation-ok");
        return 0;
    }

    private static bool CanRead(string path)
    {
        try
        {
            using (FileStream stream = new FileStream(
                path,
                FileMode.Open,
                FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete))
            {
                return stream.ReadByte() >= -1;
            }
        }
        catch (UnauthorizedAccessException)
        {
            return false;
        }
        catch (DirectoryNotFoundException)
        {
            return false;
        }
        catch (FileNotFoundException)
        {
            return false;
        }
        catch (IOException)
        {
            return false;
        }
    }

    private static bool CanConnect(IPAddress address, int port, int timeoutMs)
    {
        Socket socket = null;
        try
        {
            socket = new Socket(
                address.AddressFamily,
                SocketType.Stream,
                ProtocolType.Tcp);
            IAsyncResult pending = socket.BeginConnect(address, port, null, null);
            if (!pending.AsyncWaitHandle.WaitOne(timeoutMs))
            {
                return false;
            }
            socket.EndConnect(pending);
            return socket.Connected;
        }
        catch (SocketException)
        {
            return false;
        }
        catch (NotSupportedException)
        {
            return false;
        }
        finally
        {
            if (socket != null)
            {
                socket.Close();
            }
        }
    }

    private static bool CanResolve(string host)
    {
        try
        {
            IPAddress[] addresses = Dns.GetHostAddresses(host);
            return addresses.Length != 0;
        }
        catch (SocketException)
        {
            return false;
        }
    }

    private static int StartTree()
    {
        Process child = StartSelf("tree-child");
        Console.WriteLine(
            "tree-root={0};child={1}",
            Process.GetCurrentProcess().Id,
            child.Id);
        Console.Out.Flush();
        Thread.Sleep(120000);
        return 0;
    }

    private static int StartTreeChild()
    {
        Process grandchild = StartSelf("tree-grandchild");
        File.WriteAllText(
            Path.Combine(EffectiveScratchRoot, "tree-ready.txt"),
            grandchild.Id.ToString(CultureInfo.InvariantCulture),
            new UTF8Encoding(false));
        Console.WriteLine(
            "tree-child={0};grandchild={1}",
            Process.GetCurrentProcess().Id,
            grandchild.Id);
        Console.Out.Flush();
        Thread.Sleep(120000);
        return 0;
    }

    private static Process StartSelf(string mode)
    {
        ProcessStartInfo start = new ProcessStartInfo();
        start.FileName = Assembly.GetExecutingAssembly().Location;
        start.Arguments = mode;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        Process process = Process.Start(start);
        if (process == null)
        {
            throw new InvalidOperationException("child process was not created");
        }
        return process;
    }

    private static int FloodOutput()
    {
        string block = new string('X', 8192);
        while (true)
        {
            Console.Out.Write(block);
            Console.Error.Write(block);
        }
    }

    private static int ExhaustMemory()
    {
        List<byte[]> allocations = new List<byte[]>();
        try
        {
            while (true)
            {
                byte[] block = new byte[8 * 1024 * 1024];
                block[0] = 1;
                block[block.Length - 1] = 1;
                allocations.Add(block);
            }
        }
        catch (OutOfMemoryException)
        {
            allocations.Clear();
            GC.Collect();
            Console.WriteLine("memory-limit-observed");
            return 0;
        }
    }

    private static int CheckProcessLimit()
    {
        List<Process> children = new List<Process>();
        bool blocked = false;
        try
        {
            for (int index = 0; index < 32; index++)
            {
                try
                {
                    children.Add(StartSelf("sleep"));
                }
                catch
                {
                    blocked = true;
                    break;
                }
            }
            Console.WriteLine(
                "process-limit-observed={0};started={1}",
                blocked ? "true" : "false",
                children.Count);
            return blocked && children.Count < 8 ? 0 : 1;
        }
        finally
        {
            foreach (Process child in children)
            {
                child.Dispose();
            }
        }
    }

    private static int CheckTokenBoundary()
    {
        List<string> failures = new List<string>();
        IntPtr token = IntPtr.Zero;
        try
        {
            if (!OpenProcessToken(Process.GetCurrentProcess().Handle, 0x0008, out token)
                || !IsTokenRestricted(token)
                || ReadIntegrityRid(token) > 4096)
            {
                failures.Add("token-not-restricted-low");
            }
        }
        finally
        {
            CloseNativeHandle(token);
        }

        string scratch = Path.Combine(EffectiveScratchRoot, "low-token-write.txt");
        try
        {
            File.WriteAllText(scratch, "ok", new UTF8Encoding(false));
            File.Delete(scratch);
        }
        catch
        {
            failures.Add("scratch-write-denied");
        }
        if (CanWrite(Path.Combine(GuestResultRoot, "forged-result.json")))
        {
            failures.Add("result-forgery");
        }
        if (CanWrite(GuestRequestPath))
        {
            failures.Add("control-forgery");
        }

        int runnerPid;
        if (!Int32.TryParse(
            Environment.GetEnvironmentVariable("NEIL_RUNNER_PID"),
            NumberStyles.None,
            CultureInfo.InvariantCulture,
            out runnerPid))
        {
            failures.Add("runner-pid-missing");
        }
        else
        {
            IntPtr process = OpenProcess(
                ProcessCreateThread | ProcessVmOperation | ProcessVmWrite,
                false,
                runnerPid);
            if (process != IntPtr.Zero)
            {
                failures.Add("runner-openprocess-write");
                CloseNativeHandle(process);
            }
        }
        return Report("token-boundary-ok", failures);
    }

    private static int CheckBrokerEscape()
    {
        List<string> failures = new List<string>();
        IntPtr manager = OpenSCManager(null, null, ScManagerCreateService);
        if (manager != IntPtr.Zero)
        {
            failures.Add("scm-create-service");
            CloseServiceHandle(manager);
        }

        string taskName = "NeilAgentEscape-" + Process.GetCurrentProcess().Id.ToString(
            CultureInfo.InvariantCulture);
        if (RunBrokerCommand(
            Path.Combine(Environment.SystemDirectory, "schtasks.exe"),
            "/Create /F /SC ONCE /ST 23:59 /TN \"" + taskName
            + "\" /TR \"cmd.exe /c exit 0\""))
        {
            failures.Add("task-scheduler-create");
            RunBrokerCommand(
                Path.Combine(Environment.SystemDirectory, "schtasks.exe"),
                "/Delete /F /TN \"" + taskName + "\"");
        }

        string powershell = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            @"WindowsPowerShell\v1.0\powershell.exe");
        string wmiCommand =
            "-NoLogo -NoProfile -NonInteractive -Command \""
            + "$r=Invoke-WmiMethod -Class Win32_Process -Name Create "
            + "-ArgumentList 'cmd.exe /c exit 0';"
            + "if($r.ReturnValue -eq 0){exit 0}else{exit 1}\"";
        if (RunBrokerCommand(powershell, wmiCommand))
        {
            failures.Add("wmi-process-create");
        }
        return Report("broker-escape-blocked", failures);
    }

    private static int CheckBreakaway()
    {
        STARTUPINFO startup = new STARTUPINFO();
        startup.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        PROCESS_INFORMATION process = new PROCESS_INFORMATION();
        string executable = Assembly.GetExecutingAssembly().Location;
        StringBuilder commandLine = new StringBuilder(
            "\"" + executable + "\" sleep");
        bool created = CreateProcess(
            executable,
            commandLine,
            IntPtr.Zero,
            IntPtr.Zero,
            false,
            CreateBreakawayFromJob | CreateNoWindow,
            IntPtr.Zero,
            Environment.CurrentDirectory,
            ref startup,
            out process);
        if (created)
        {
            TerminateProcess(process.hProcess, 91);
            CloseNativeHandle(process.hThread);
            CloseNativeHandle(process.hProcess);
            Console.Error.WriteLine("breakaway-created");
            return 1;
        }
        Console.WriteLine("breakaway-blocked");
        return 0;
    }

    private static int CheckAggregateJobMemory()
    {
        List<Process> children = new List<Process>();
        try
        {
            for (int index = 0; index < 3; index++)
            {
                try
                {
                    children.Add(StartSelf("memory-hold 48"));
                }
                catch
                {
                    Console.WriteLine("job-memory-limit-observed=start-denied");
                    return 0;
                }
            }
            Stopwatch wait = Stopwatch.StartNew();
            while (wait.ElapsedMilliseconds < 15000)
            {
                foreach (Process child in children)
                {
                    if (child.HasExited)
                    {
                        Console.WriteLine("job-memory-limit-observed=terminated");
                        return 0;
                    }
                }
                Thread.Sleep(25);
            }
            Console.Error.WriteLine("aggregate-job-memory-not-enforced");
            return 1;
        }
        finally
        {
            foreach (Process child in children)
            {
                child.Dispose();
            }
        }
    }

    private static int HoldMemory(string[] args)
    {
        int mebibytes = args.Length > 1
            ? Int32.Parse(args[1], CultureInfo.InvariantCulture)
            : 48;
        byte[] allocation = new byte[mebibytes * 1024 * 1024];
        for (int index = 0; index < allocation.Length; index += 4096)
        {
            allocation[index] = 1;
        }
        Console.WriteLine("memory-ready");
        Console.Out.Flush();
        Thread.Sleep(120000);
        GC.KeepAlive(allocation);
        return 0;
    }

    private static int Report(string success, List<string> failures)
    {
        if (failures.Count != 0)
        {
            Console.Error.WriteLine(String.Join(",", failures.ToArray()));
            return 1;
        }
        Console.WriteLine(success);
        return 0;
    }

    private static bool CanWrite(string path)
    {
        try
        {
            using (FileStream stream = new FileStream(
                path,
                FileMode.OpenOrCreate,
                FileAccess.Write,
                FileShare.ReadWrite | FileShare.Delete))
            {
                stream.WriteByte(1);
            }
            return true;
        }
        catch (UnauthorizedAccessException)
        {
            return false;
        }
        catch (IOException)
        {
            return false;
        }
    }

    private static int ReadIntegrityRid(IntPtr token)
    {
        uint required = 0;
        GetTokenInformation(token, TokenIntegrityLevel, IntPtr.Zero, 0, out required);
        if (required == 0)
        {
            return Int32.MaxValue;
        }
        IntPtr buffer = Marshal.AllocHGlobal(unchecked((int)required));
        try
        {
            if (!GetTokenInformation(
                token,
                TokenIntegrityLevel,
                buffer,
                required,
                out required))
            {
                return Int32.MaxValue;
            }
            TOKEN_MANDATORY_LABEL label = (TOKEN_MANDATORY_LABEL)
                Marshal.PtrToStructure(buffer, typeof(TOKEN_MANDATORY_LABEL));
            IntPtr countPointer = GetSidSubAuthorityCount(label.Label.Sid);
            byte count = Marshal.ReadByte(countPointer);
            IntPtr ridPointer = GetSidSubAuthority(label.Label.Sid, count - 1);
            return Marshal.ReadInt32(ridPointer);
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    private static bool RunBrokerCommand(string executable, string arguments)
    {
        try
        {
            ProcessStartInfo start = new ProcessStartInfo();
            start.FileName = executable;
            start.Arguments = arguments;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            using (Process process = Process.Start(start))
            {
                if (process == null || !process.WaitForExit(20000))
                {
                    if (process != null)
                    {
                        process.Kill();
                    }
                    return false;
                }
                return process.ExitCode == 0;
            }
        }
        catch
        {
            return false;
        }
    }

    private static void CloseNativeHandle(IntPtr handle)
    {
        if (handle != IntPtr.Zero && handle != new IntPtr(-1))
        {
            CloseHandle(handle);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SID_AND_ATTRIBUTES
    {
        internal IntPtr Sid;
        internal uint Attributes;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct TOKEN_MANDATORY_LABEL
    {
        internal SID_AND_ATTRIBUTES Label;
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

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(
        IntPtr processHandle,
        uint desiredAccess,
        out IntPtr tokenHandle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool IsTokenRestricted(IntPtr tokenHandle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(
        IntPtr tokenHandle,
        int tokenInformationClass,
        IntPtr tokenInformation,
        uint tokenInformationLength,
        out uint returnLength);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern IntPtr GetSidSubAuthority(IntPtr sid, int subAuthority);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(
        uint desiredAccess,
        bool inheritHandle,
        int processId);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr OpenSCManager(
        string machineName,
        string databaseName,
        uint desiredAccess);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool CloseServiceHandle(IntPtr serviceHandle);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(
        string applicationName,
        StringBuilder commandLine,
        IntPtr processAttributes,
        IntPtr threadAttributes,
        bool inheritHandles,
        uint creationFlags,
        IntPtr environment,
        string currentDirectory,
        ref STARTUPINFO startupInfo,
        out PROCESS_INFORMATION processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);
}
