// DSH 多版本启动器 —— 轻量启动壳（tiny launcher）
// 作用：自己不起控制台，找到本机 Python，用 pythonw 静默拉起同目录的 dsh_lanes_gui.py。
// 编译：csc /target:winexe /win32icon:app.ico /out:xxx.exe dsh_stub.cs
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;
using Microsoft.Win32;

static class Stub
{
    const string Script = "dsh_lanes_gui.py";

    static int Main(string[] argv)
    {
        try
        {
            string dir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            string script = Path.Combine(dir, Script);
            if (!File.Exists(script))
            {
                Fail("找不到同目录下的 " + Script + "\n\n" + dir);
                return 3;
            }

            List<string> candidates = new List<string>();

            string env = Environment.GetEnvironmentVariable("DSH_LANES_PY");
            if (!string.IsNullOrEmpty(env)) candidates.Add(env);

            // 1) pyw.exe（Windows 官方 launcher，GUI 子系统）
            foreach (string p in Where("pyw.exe")) candidates.Add(p + "|-3");

            // 2) 注册表里的 Python 3 安装路径 → pythonw.exe
            foreach (string hive in new string[] { "HKCU", "HKLM" })
            {
                foreach (string ver in new string[] {
                    "3.14", "3.13", "3.12", "3.11", "3.10", "3.9", "3.8" })
                {
                    string exe = ReadReg(hive, @"SOFTWARE\Python\PythonCore\" + ver + @"\InstallPath",
                                         "ExecutablePath");
                    if (!string.IsNullOrEmpty(exe))
                    {
                        string pw = Path.Combine(Path.GetDirectoryName(exe), "pythonw.exe");
                        if (File.Exists(pw)) candidates.Add(pw);
                    }
                }
            }

            // 3) PATH 里的 pythonw.exe
            foreach (string p in Where("pythonw.exe")) candidates.Add(p);

            foreach (string cand in candidates)
            {
                string exe = cand;
                string prefix = "";
                int bar = cand.IndexOf('|');
                if (bar >= 0) { exe = cand.Substring(0, bar); prefix = cand.Substring(bar + 1); }

                string quotedScript = "\"" + script + "\"";
                string args = prefix.Length > 0 ? prefix + " " + quotedScript : quotedScript;
                foreach (string a in argv)
                    args += " \"" + a.Replace("\"", "\\\"") + "\"";

                ProcessStartInfo psi = new ProcessStartInfo(exe, args);
                psi.WorkingDirectory = dir;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;

                Process p = Process.Start(psi);
                if (p == null) continue;

                // 立刻退出 = 解释器起不来（缺 tkinter / 版本不对 …），换下一个候选
                if (p.WaitForExit(1200))
                {
                    if (p.ExitCode == 0) return 0;
                    continue;
                }
                return 0;
            }

            Fail("没找到可用的 Python 3（需要带 tkinter）。\n\n"
               + "已尝试：pyw.exe / 注册表里的 Python 3 / PATH 里的 pythonw.exe\n\n"
               + "装一个 Python 3 就行，或者设置环境变量 DSH_LANES_PY 指向 pythonw.exe。");
            return 2;
        }
        catch (Exception ex)
        {
            Fail(ex.GetType().Name + ": " + ex.Message);
            return 1;
        }
    }

    static IEnumerable<string> Where(string name)
    {
        string path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string raw in path.Split(';'))
        {
            string d = raw.Trim().Trim('"');
            if (d.Length == 0) continue;
            string f;
            try { f = Path.Combine(d, name); } catch { continue; }
            if (File.Exists(f)) yield return f;
        }
    }

    static string ReadReg(string hive, string sub, string value)
    {
        try
        {
            RegistryKey root = hive == "HKCU" ? Registry.CurrentUser : Registry.LocalMachine;
            using (RegistryKey k = root.OpenSubKey(sub))
            {
                if (k == null) return null;
                return k.GetValue(value) as string;
            }
        }
        catch { return null; }
    }

    static void Fail(string msg)
    {
        MessageBox.Show(msg, "DSH 多版本启动器", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
