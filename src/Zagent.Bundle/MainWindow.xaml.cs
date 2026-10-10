using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows.Controls;
using Microsoft.Win32;
using System.Windows;
using Zagent.Shared.Installation;

namespace Zagent.Bundle;

public partial class MainWindow : Window
{
    private bool _busy;
    private bool _allowClose;
    private CancellationTokenSource? _cts;

    public MainWindow()
    {
        InitializeComponent();

        InstallPathBox.Text = Path.Combine(
            Environment.GetFolderPath(
                Environment.SpecialFolder.LocalApplicationData),
            "Programs");

        InstallPathBox.TextChanged += (_, _) =>
            RefreshMode();
        PluginRoleBox.SelectionChanged += (_, _) => RefreshMode();

        RefreshMode();
    }

    private void RefreshMode()
    {
        var baseRoot = InstallPathBox.Text.Trim();
        var target = ResolveTargetRoot(baseRoot);
        var installed = IsExistingZagentRoot(target);
        var existingAudience =
            TryReadExistingPluginAudience(target);

        if (!string.IsNullOrWhiteSpace(
                existingAudience))
        {
            SelectPluginAudience(
                existingAudience);
            PluginRoleBox.IsEnabled = false;
        }
        else
        {
            PluginRoleBox.IsEnabled = !_busy;
        }

        if (!_busy)
        {
            StatusText.Text =
                installed
                    ? "Instalasi Zagent terdeteksi di " + target +
                      ". Install akan memperbarui program di tempat yang sama dan mempertahankan data/workspace." +
                      (!string.IsNullOrWhiteSpace(existingAudience)
                        ? " Plugin role existing dipertahankan."
                        : "")
                    : "Target instalasi: " + target;

            ModeText.Text =
                installed
                    ? "Mode: in-place replace/update"
                    : "Mode: fresh install";
        }

        InstallButton.Content = "Install";
    }

    private void BrowseButton_Click(
        object sender,
        RoutedEventArgs e)
    {
        if (_busy)
        {
            return;
        }

        var dialog = new OpenFolderDialog
        {
            Title = "Pilih base folder instalasi Zagent",
            Multiselect = false
        };

        var current = InstallPathBox.Text.Trim();
        if (Directory.Exists(current))
        {
            dialog.InitialDirectory = current;
        }

        if (dialog.ShowDialog(this) == true)
        {
            InstallPathBox.Text = dialog.FolderName;
        }
    }

    private async void InstallButton_Click(
        object sender,
        RoutedEventArgs e) =>
        await ExecuteInstallAsync();

    private void CancelButton_Click(
        object sender,
        RoutedEventArgs e)
    {
        if (_busy)
        {
            CancelButton.IsEnabled = false;
            StatusText.Text = "Membatalkan...";
            _cts?.Cancel();
            return;
        }

        _allowClose = true;
        Close();
    }

    private async Task ExecuteInstallAsync()
    {
        if (_busy)
        {
            return;
        }

        var baseRoot = InstallPathBox.Text.Trim();
        if (string.IsNullOrWhiteSpace(baseRoot))
        {
            MessageBox.Show(
                this,
                "Pilih base folder instalasi terlebih dahulu.",
                "Zagent Setup",
                MessageBoxButton.OK,
                MessageBoxImage.Warning);
            return;
        }

        var target = ResolveTargetRoot(baseRoot);
        var selectedAudience =
            GetSelectedPluginAudience();

        var previousRole = Environment.GetEnvironmentVariable("ZAGENT_INSTALLATION_ROLE");
        var selectedRole = ZagentInstallationProfile.FromAudience(selectedAudience);
        var previousAudience =
            Environment.GetEnvironmentVariable(
                "ZAGENT_PLUGIN_AUDIENCE");
        var previousDisplay =
            Environment.GetEnvironmentVariable(
                "ZAGENT_PLUGIN_DISPLAY_NAME");

        Environment.SetEnvironmentVariable("ZAGENT_INSTALLATION_ROLE", selectedRole);
        Environment.SetEnvironmentVariable(
            "ZAGENT_PLUGIN_AUDIENCE",
            selectedAudience);
        Environment.SetEnvironmentVariable(
            "ZAGENT_PLUGIN_DISPLAY_NAME",
            ZagentInstallationProfile.DisplayName(selectedRole));

        _busy = true;
        _cts = new CancellationTokenSource();
        ToggleUiForOperation(running: true);

        string? payloadRoot = null;
        var closeAfterManagerLaunch = false;

        try
        {
            var progress = new Progress<(int Percent, string Message)>(
                value =>
                {
                    Progress.Value = value.Percent;
                    StatusText.Text = value.Message;
                });

            payloadRoot = await BundlePayloadReader.ExtractAsync(
                progress,
                _cts.Token);

            var result = await SetupRunner.RunAsync(
                payloadRoot,
                "install",
                target,
                progress,
                _cts.Token);

            if (!result.Success)
            {
                throw new InvalidOperationException(
                    result.Message);
            }

            Progress.Value = 100;
            StatusText.Text =
                "Instalasi selesai. Membuka Zagent Manager...";

            closeAfterManagerLaunch =
                LaunchManager(target);

            if (!closeAfterManagerLaunch)
            {
                throw new InvalidOperationException(
                    "Instalasi selesai, tetapi Zagent Manager tidak dapat dibuka.");
            }
        }
        catch (OperationCanceledException)
        {
            StatusText.Text = "Operasi dibatalkan.";
        }
        catch (Exception ex)
        {
            StatusText.Text = "Gagal: " + ex.Message;
            MessageBox.Show(
                this,
                ex.Message,
                "Zagent Setup",
                MessageBoxButton.OK,
                MessageBoxImage.Error);
        }
        finally
        {
            BundlePayloadReader.Cleanup(payloadRoot);

            Environment.SetEnvironmentVariable("ZAGENT_INSTALLATION_ROLE", previousRole);
            Environment.SetEnvironmentVariable(
                "ZAGENT_PLUGIN_AUDIENCE",
                previousAudience);
            Environment.SetEnvironmentVariable(
                "ZAGENT_PLUGIN_DISPLAY_NAME",
                previousDisplay);

            _cts?.Dispose();
            _cts = null;
            _busy = false;

            if (!closeAfterManagerLaunch)
            {
                ToggleUiForOperation(running: false);
                RefreshMode();
            }
        }

        if (closeAfterManagerLaunch)
        {
            _allowClose = true;
            Close();
        }
    }

    private string GetSelectedPluginAudience()
    {
        if (PluginRoleBox.SelectedItem is ComboBoxItem item &&
            item.Tag is string audience &&
            !string.IsNullOrWhiteSpace(audience))
        {
            return audience.Trim().ToLowerInvariant();
        }

        return "zagent-production";
    }

    private void SelectPluginAudience(string audience)
    {
        var normalized =
            (audience ?? "").Trim().ToLowerInvariant();

        foreach (var entry in PluginRoleBox.Items)
        {
            if (entry is ComboBoxItem item &&
                item.Tag is string tag &&
                string.Equals(
                    tag,
                    normalized,
                    StringComparison.OrdinalIgnoreCase))
            {
                PluginRoleBox.SelectedItem = item;
                return;
            }
        }
    }

    private static string? TryReadExistingPluginAudience(
        string target)
    {
        if (string.IsNullOrWhiteSpace(target))
        {
            return null;
        }

        var path =
            Path.Combine(
                Path.GetFullPath(target),
                "data",
                "config",
                "plugin-access.json");

        if (!File.Exists(path))
        {
            return null;
        }

        try
        {
            using var stream =
                File.OpenRead(path);
            using var document =
                JsonDocument.Parse(stream);

            if (document.RootElement.TryGetProperty(
                    "audience",
                    out var audience) &&
                audience.ValueKind == JsonValueKind.String)
            {
                return audience.GetString();
            }
        }
        catch
        {
        }

        return null;
    }

    private static bool LaunchManager(string target)
    {
        var manager = Path.Combine(
            Path.GetFullPath(target),
            "Zagent.Manager.exe");

        if (!File.Exists(manager))
        {
            return false;
        }

        var process = Process.Start(
            new ProcessStartInfo
            {
                FileName = manager,
                WorkingDirectory =
                    Path.GetDirectoryName(manager)!,
                UseShellExecute = true
            });

        return process is not null;
    }

    private string ResolveTargetRoot(
        string baseRoot)
    {
        if (string.IsNullOrWhiteSpace(baseRoot))
        {
            return "";
        }

        var fullBase = Path.GetFullPath(baseRoot);

        // If the user selected an existing Zagent install root itself,
        // update it in place rather than producing <Zagent>\Zagent.
        if (IsExistingZagentRoot(fullBase))
        {
            return fullBase;
        }

        return InstallTargetResolver.ResolveFromBaseFolder(
            fullBase,
            ZagentInstallationProfile.FromAudience(GetSelectedPluginAudience()));
    }

    private static bool IsExistingZagentRoot(string? path)
    {
        if (string.IsNullOrWhiteSpace(path) ||
            !Directory.Exists(path))
        {
            return false;
        }

        var full = Path.GetFullPath(path);

        if (File.Exists(
                Path.Combine(
                    full,
                    "payload-manifest.json")))
        {
            return true;
        }

        var manager = File.Exists(
            Path.Combine(
                full,
                "Zagent.Manager.exe"));
        var core = File.Exists(
            Path.Combine(
                full,
                "Zagent.Core.exe"));
        var agents = Directory.Exists(
            Path.Combine(
                full,
                "agents"));
        var machineConfig = File.Exists(
            Path.Combine(
                full,
                "data",
                "config",
                "zagent.machine.json"));

        return (manager || core) &&
            (agents || machineConfig);
    }

    private void ToggleUiForOperation(bool running)
    {
        InstallPathBox.IsEnabled = !running;
        BrowseButton.IsEnabled = !running;
        PluginRoleBox.IsEnabled = !running &&
            string.IsNullOrWhiteSpace(
                TryReadExistingPluginAudience(
                    ResolveTargetRoot(
                        InstallPathBox.Text.Trim())));
        InstallButton.IsEnabled = !running;
        InstallButton.Visibility =
            running
                ? Visibility.Collapsed
                : Visibility.Visible;
        CancelButton.IsEnabled = true;
        ModeText.Text =
            running
                ? "Installer sedang bekerja..."
                : ModeText.Text;
    }

    protected override void OnClosing(
        System.ComponentModel.CancelEventArgs e)
    {
        if (_busy && !_allowClose)
        {
            var answer = MessageBox.Show(
                this,
                "Instalasi sedang berjalan. Batalkan dan tutup?",
                "Zagent Setup",
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning);

            if (answer != MessageBoxResult.Yes)
            {
                e.Cancel = true;
                return;
            }

            _cts?.Cancel();
        }

        base.OnClosing(e);
    }
}
