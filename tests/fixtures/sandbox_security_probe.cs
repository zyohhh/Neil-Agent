using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;

internal static class SandboxSecurityProbe
{
    private const string GuestExportRoot = @"C:\NeilAgent\Export";
    private const string GuestScratchRoot = @"C:\NeilAgent\Scratch";

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
            if (!String.Equals(value, GuestScratchRoot, StringComparison.OrdinalIgnoreCase))
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
}
