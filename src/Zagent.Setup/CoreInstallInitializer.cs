using System.Diagnostics;

namespace Zagent.Setup;

public sealed class CoreInstallInitializer : IInstallInitializer
{
    public async Task InitializeAsync(
        string targetRoot,
        string dataRoot,
        string filesProfile,
        string productVersion,
        CancellationToken cancellationToken)
    {
        var core = Path.Combine(
            targetRoot,
            "Zagent.Core.exe");

        if (!File.Exists(core))
        {
            throw new FileNotFoundException(
                "Installed Zagent.Core.exe was not found.",
                core);
        }

        var info = new ProcessStartInfo
        {
            FileName = core,
            WorkingDirectory = targetRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };

        foreach (var arg in new[]
        {
            "--initialize-install",
            "--root",
            targetRoot,
            "--data-root",
            dataRoot,
            "--files-profile",
            filesProfile,
            "--product-version",
            productVersion
        })
        {
            info.ArgumentList.Add(arg);
        }

        using var process =
            Process.Start(info) ??
            throw new InvalidOperationException(
                "Could not start Zagent.Core install initializer.");

        var stdoutTask =
            process.StandardOutput.ReadToEndAsync(
                cancellationToken);
        var stderrTask =
            process.StandardError.ReadToEndAsync(
                cancellationToken);

        await process.WaitForExitAsync(
            cancellationToken);

        var stdout = await stdoutTask;
        var stderr = await stderrTask;

        if (process.ExitCode != 0)
        {
            throw new InvalidOperationException(
                "Zagent Core install initialization failed: " +
                (string.IsNullOrWhiteSpace(stderr)
                    ? stdout
                    : stderr));
        }
        ArcMapAddInIntegration.Install(targetRoot, dataRoot);
    }
}