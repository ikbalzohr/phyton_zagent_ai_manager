using System.Text.Json;
using Zagent.Shared.Manager;

namespace Zagent.Core.Manager;

public sealed class ManagerChatLogReader
{
    private readonly string _path;

    public ManagerChatLogReader(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);
        _path = Path.GetFullPath(path);
    }

    public async Task<IReadOnlyList<ManagerChatLogRow>> ReadAsync(
        int limit,
        IReadOnlyList<ManagerChatRow> chats,
        CancellationToken cancellationToken = default)
    {
        if (!File.Exists(_path))
        {
            return Array.Empty<ManagerChatLogRow>();
        }

        var lookup = chats.ToDictionary(
            item => item.SessionId,
            item => item,
            StringComparer.Ordinal);

        var lines = await File.ReadAllLinesAsync(
            _path,
            cancellationToken);

        var rows = new List<ManagerChatLogRow>();

        foreach (var line in lines.Reverse().Take(
                     Math.Clamp(limit, 1, 2000)))
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            try
            {
                using var document = JsonDocument.Parse(line);
                var root = document.RootElement;

                var session = String(root, "chat_id") ??
                    String(root, "session_id") ?? "";
                var alias = String(root, "chat_alias") ?? "";
                var title = String(root, "chat_title") ?? "";

                if (lookup.TryGetValue(session, out var chat))
                {
                    if (string.IsNullOrWhiteSpace(alias))
                    {
                        alias = chat.Alias;
                    }

                    if (string.IsNullOrWhiteSpace(title))
                    {
                        title = chat.Title;
                    }
                }

                var display = string.IsNullOrWhiteSpace(title)
                    ? alias
                    : $"{alias} · {title}";

                rows.Add(
                    new ManagerChatLogRow(
                        session,
                        alias,
                        title,
                        String(root, "source") ?? "",
                        String(root, "agent") ?? "",
                        String(root, "tool") ?? "",
                        String(root, "message") ??
                            String(root, "text") ??
                            "",
                        Timestamp(root, "timestamp") ??
                            Timestamp(root, "time"),
                        display));
            }
            catch (JsonException)
            {
            }
        }

        return rows;
    }

    private static string? String(
        JsonElement root,
        string name)
    {
        if (!root.TryGetProperty(name, out var value))
        {
            return null;
        }

        return value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : value.ToString();
    }

    private static double? Timestamp(
        JsonElement root,
        string name)
    {
        if (!root.TryGetProperty(name, out var value))
        {
            return null;
        }

        if (value.ValueKind == JsonValueKind.Number &&
            value.TryGetDouble(out var number))
        {
            return number;
        }

        if (value.ValueKind != JsonValueKind.String)
        {
            return null;
        }

        var text = value.GetString();

        if (double.TryParse(
                text,
                System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture,
                out number))
        {
            return number;
        }

        if (DateTimeOffset.TryParse(
                text,
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.RoundtripKind,
                out var timestamp))
        {
            return timestamp.ToUnixTimeMilliseconds() / 1000d;
        }

        return null;
    }
}
