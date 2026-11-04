using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace AeonMudLauncher;

internal sealed record SavedProfile(string Name, string Host, int Port, string[] Characters)
{
    public override string ToString() => $"{Name}  —  {Host}:{Port}";
}

internal static class Launcher
{
    internal static readonly string Root = AppContext.BaseDirectory;
    internal static string ProfilesPath =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            "AeonMud", "client", "profiles.json");

    internal static IReadOnlyList<SavedProfile> ReadProfiles(string path)
    {
        if (!File.Exists(path)) return [];
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        if (!doc.RootElement.TryGetProperty("profiles", out var profiles) ||
            profiles.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("Expected a profiles object in profiles.json.");
        var result = new List<SavedProfile>();
        foreach (var entry in profiles.EnumerateObject())
        {
            if (entry.Value.ValueKind != JsonValueKind.Object) continue;
            var p = entry.Value;
            string host = p.TryGetProperty("host", out var h) && h.ValueKind == JsonValueKind.String
                ? h.GetString() ?? "" : "";
            int port = p.TryGetProperty("port", out var n) && n.TryGetInt32(out int value) ? value : 4000;
            var characters = new List<string>();
            if (p.TryGetProperty("characters", out var list) && list.ValueKind == JsonValueKind.Array)
                foreach (var ch in list.EnumerateArray())
                    if (ch.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(ch.GetString()))
                        characters.Add(ch.GetString()!);
            result.Add(new SavedProfile(entry.Name, host, port, [..characters]));
        }
        return result.OrderBy(p => p.Name, StringComparer.OrdinalIgnoreCase).ToList();
    }

    internal static ProcessStartInfo BuildProcess(string[] args, string? root = null)
    {
        root ??= Root;
        var script = Path.Combine(root, "aeon-mud-wezterm.ps1");
        var client = Path.Combine(root, "aeon_mud_client.py");
        if (!File.Exists(client) || !File.Exists(script))
            throw new FileNotFoundException(
                "Place aeon-mud.exe alongside aeon_mud_client.py, aeon_mud/, and the existing Windows launchers. " +
                "The executable does not replace the Python client.");
        var info = new ProcessStartInfo("powershell.exe")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = root
        };
        foreach (var part in new[] { "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script })
            info.ArgumentList.Add(part);
        foreach (var part in args) info.ArgumentList.Add(part);
        return info;
    }

    internal static async Task<(int Code, string Output)> Invoke(string[] args)
    {
        using var process = new Process { StartInfo = BuildProcess(args) };
        if (!process.Start()) throw new InvalidOperationException("Could not launch PowerShell.");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        return (process.ExitCode, (await stdout + Environment.NewLine + await stderr).Trim());
    }

    internal static void Error(string text) =>
        MessageBox.Show(text, "Aeon MUD", MessageBoxButtons.OK, MessageBoxIcon.Error);

    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        if (args.Length == 1 && args[0] == "--launcher-self-test")
        {
            // Headless CI coverage: no GUI, no profile writes, no network.
            try
            {
                using var fixture = JsonDocument.Parse(
                    "{\"profiles\":{\"A profile\":{\"host\":\"example.test\",\"port\":2069,\"characters\":[\"A Wizard\",\"B\"]}}}");
                var temp = Path.GetTempFileName();
                try
                {
                    File.WriteAllText(temp, fixture.RootElement.GetRawText());
                    var p = ReadProfiles(temp).Single();
                    if (p.Name != "A profile" || p.Port != 2069 || p.Characters[0] != "A Wizard")
                        throw new Exception("Profile parsing mismatch");
                    var test = BuildProcess(["connect", "A profile", "--login", "A Wizard"], Directory.GetCurrentDirectory());
                    if (!test.ArgumentList.Contains("A profile") || !test.ArgumentList.Contains("A Wizard"))
                        throw new Exception("Argument boundaries lost");
                }
                finally { File.Delete(temp); }
                Environment.ExitCode = 0;
            }
            catch (Exception)
            {
                Environment.ExitCode = 1;
            }
            return;
        }
        if (args.Length > 0)
        {
            // Command-specific help runs in the existing plain client, without WezTerm.
            // Non-connect commands retain the canonical CLI, rather than opening a MUD window.
            Application.Run(new CommandWindow(args));
        }
        else Application.Run(new PickerWindow());
    }
}

internal sealed class CommandWindow : Form
{
    private readonly TextBox output = new()
    {
        Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both,
        Dock = DockStyle.Fill, Font = new System.Drawing.Font("Consolas", 10),
        WordWrap = false
    };

    public CommandWindow(string[] args)
    {
        Text = "Aeon MUD — command";
        Width = 780; Height = 440;
        Controls.Add(output);
        Shown += async (_, _) =>
        {
            try
            {
                // Non-connect CLI commands (including help) run in Python directly.
                // This provides output without opening an unnecessary WezTerm session.
                bool connect = args[0].Equals("connect", StringComparison.OrdinalIgnoreCase)
                    && !args.Any(a => a is "help" or "-h" or "--help");
                var info = connect ? Launcher.BuildProcess(args) :
                    new ProcessStartInfo("python.exe")
                    {
                        WorkingDirectory = Launcher.Root,
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        RedirectStandardOutput = true,
                        RedirectStandardError = true
                    };
                if (!connect)
                {
                    info.ArgumentList.Add(Path.Combine(Launcher.Root, "aeon_mud_client.py"));
                    foreach (var arg in args) info.ArgumentList.Add(arg);
                }
                using var process = new Process { StartInfo = info };
                process.Start();
                var stdout = process.StandardOutput.ReadToEndAsync();
                var stderr = process.StandardError.ReadToEndAsync();
                await process.WaitForExitAsync();
                output.Text = (await stdout + Environment.NewLine + await stderr).Trim();
                Text = $"Aeon MUD — exit {process.ExitCode}";
            }
            catch (Exception e) { Launcher.Error(e.Message); Close(); }
        };
    }
}

internal sealed class PickerWindow : Form
{
    private readonly ComboBox profiles = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 460 };
    private readonly ComboBox characters = new() { DropDownStyle = ComboBoxStyle.DropDownList, Width = 460 };
    private readonly Label hint = new() { AutoSize = true, MaximumSize = new System.Drawing.Size(480, 0) };
    private readonly Button connect = new() { Text = "Connect", Width = 110 };
    private readonly Button refresh = new() { Text = "Refresh", Width = 90 };
    private readonly Button newConnection = new() { Text = "New connection…", Width = 150 };

    public PickerWindow()
    {
        Text = "Aeon MUD";
        Width = 545; Height = 310;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        var layout = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown,
            WrapContents = false, Padding = new Padding(18), AutoScroll = true
        };
        layout.Controls.Add(new Label { Text = "Saved profile", AutoSize = true });
        layout.Controls.Add(profiles);
        layout.Controls.Add(new Label { Text = "Character (optional)", AutoSize = true, Margin = new Padding(3, 14, 3, 3) });
        layout.Controls.Add(characters);
        hint.Margin = new Padding(3, 12, 3, 8);
        layout.Controls.Add(hint);
        var buttons = new FlowLayoutPanel { Width = 485, Height = 42 };
        buttons.Controls.Add(connect);
        buttons.Controls.Add(refresh);
        buttons.Controls.Add(newConnection);
        layout.Controls.Add(buttons);
        Controls.Add(layout);
        profiles.SelectedIndexChanged += (_, _) => UpdateCharacters();
        refresh.Click += (_, _) => LoadProfiles();
        connect.Click += async (_, _) => await Connect();
        newConnection.Click += async (_, _) => await NewConnection();
        Shown += (_, _) => LoadProfiles();
    }

    private void LoadProfiles()
    {
        try
        {
            profiles.Items.Clear();
            foreach (var p in Launcher.ReadProfiles(Launcher.ProfilesPath))
                profiles.Items.Add(p);
            if (profiles.Items.Count > 0) profiles.SelectedIndex = 0;
            connect.Enabled = profiles.Items.Count > 0;
            hint.Text = profiles.Items.Count == 0
                ? "No profiles saved yet. Choose New connection to enter a host and explicitly acknowledge plaintext Telnet. Existing settings are never rewritten by this picker."
                : "Choose a saved profile and optionally a character. Saved passwords remain protected by the existing Python DPAPI implementation.";
        }
        catch (Exception e)
        {
            connect.Enabled = false;
            hint.Text = "Unable to load saved profiles.";
            Launcher.Error($"Could not load {Launcher.ProfilesPath}:{Environment.NewLine}{e.Message}");
        }
    }

    private void UpdateCharacters()
    {
        characters.Items.Clear();
        characters.Items.Add("(No character login)");
        if (profiles.SelectedItem is SavedProfile profile)
            foreach (var name in profile.Characters) characters.Items.Add(name);
        characters.SelectedIndex = 0;
    }

    private async Task Connect()
    {
        if (profiles.SelectedItem is not SavedProfile profile) return;
        var args = new List<string> { "connect", profile.Name };
        if (characters.SelectedIndex > 0)
        {
            args.Add("--login");
            args.Add(characters.SelectedItem!.ToString()!);
        }
        await StartSession([..args]);
    }

    private async Task NewConnection()
    {
        using var dialog = new Form
        {
            Text = "New connection", Width = 450, Height = 270,
            StartPosition = FormStartPosition.CenterParent,
            FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false, MinimizeBox = false
        };
        var host = new TextBox { Left = 22, Top = 36, Width = 390 };
        var port = new NumericUpDown { Left = 22, Top = 96, Width = 105, Minimum = 1, Maximum = 65535, Value = 4000 };
        var consent = new CheckBox
        {
            Left = 22, Top = 140, Width = 390, Height = 40,
            Text = "I understand Telnet is unencrypted (plaintext)."
        };
        var ok = new Button { Text = "Connect", Left = 300, Top = 192, Width = 110, DialogResult = DialogResult.OK };
        dialog.Controls.AddRange([
            new Label { Text = "Hostname or IP address", Left = 22, Top = 15, AutoSize = true },
            host, new Label { Text = "Port", Left = 22, Top = 76, AutoSize = true }, port, consent, ok
        ]);
        dialog.AcceptButton = ok;
        if (dialog.ShowDialog(this) != DialogResult.OK) return;
        if (string.IsNullOrWhiteSpace(host.Text) || host.Text.Any(char.IsWhiteSpace) || !consent.Checked)
        {
            Launcher.Error("Enter a hostname without spaces and acknowledge plaintext Telnet.");
            return;
        }
        await StartSession(["connect", "--host", host.Text.Trim(), "--port",
            port.Value.ToString(System.Globalization.CultureInfo.InvariantCulture), "--allow-plaintext"]);
    }

    private async Task StartSession(string[] args)
    {
        connect.Enabled = false;
        try
        {
            var (code, output) = await Launcher.Invoke(args);
            if (code != 0) Launcher.Error($"Session launch failed (exit {code}).{Environment.NewLine}{output}");
        }
        catch (Exception e) { Launcher.Error(e.Message); }
        finally { connect.Enabled = profiles.Items.Count > 0; }
    }
}
