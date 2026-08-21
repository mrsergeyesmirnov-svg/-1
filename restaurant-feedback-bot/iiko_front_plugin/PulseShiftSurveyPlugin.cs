using System;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Resto.Front.Api;
using Resto.Front.Api.Attributes;
using Resto.Front.Api.Data.Organization;
using Resto.Front.Api.Data.Security;

namespace Pulse.IikoShiftSurvey
{
    /// <summary>
    /// Опрос на кассе до ClosePersonalSession. Без ответа Pulse смену не закрываем.
    /// Telegram не участвует. employeeId — текущий пользователь iiko, не справочник Pulse.
    /// Нужны Resto.Front.Api.Vx.dll и лицензия разработчика плагинов iiko.
    /// </summary>
    [PluginLicenseModuleId(21000000)]
    public sealed class PulseShiftSurveyPlugin : IFrontPlugin
    {
        private readonly HttpClient _http = new HttpClient { Timeout = TimeSpan.FromSeconds(8) };
        private readonly PluginConfig _cfg;

        public PulseShiftSurveyPlugin()
        {
            _cfg = PluginConfig.Load();
            PluginContext.Operations.AddButtonToPluginsMenu("Закрыть личную смену", (_, __) => CloseAfterSurvey());
        }

        public void Dispose()
        {
            _http.Dispose();
        }

        private void CloseAfterSurvey()
        {
            var user = PluginContext.Operations.GetCurrentUser();
            if (user == null)
            {
                MessageBox.Show("Нет текущего пользователя iiko.", "Pulse");
                return;
            }

            var win = new SurveyWindow();
            if (win.ShowDialog() != true)
                return;

            JObject body;
            try
            {
                body = PostSurvey(user, win.Rating, win.Blocker);
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "Pulse не ответил. Смену не закрываем.\n" + ex.Message,
                    "Pulse");
                return;
            }

            if (body["closeShift"]?.Value<bool>() != true)
            {
                MessageBox.Show("Опрос не принят. Смена остаётся открытой.", "Pulse");
                return;
            }

            ICredentials creds;
            try
            {
                creds = PluginContext.Operations.AuthenticateByPin(_cfg.ServicePin);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Служебный PIN плагина не принят iiko.\n" + ex.Message, "Pulse");
                return;
            }

            var ok = PluginContext.Operations.ClosePersonalSession(creds, user);
            if (!ok)
                MessageBox.Show("iiko не закрыл смену.", "Pulse");
        }

        private JObject PostSurvey(IUser user, int rating, string blocker)
        {
            var payload = new JObject
            {
                ["employeeId"] = user.Id.ToString(),
                ["organizationId"] = _cfg.OrganizationId,
                ["rating"] = rating,
                ["blocker"] = blocker,
                ["department"] = DepartmentOf(user)
            };
            var req = new HttpRequestMessage(HttpMethod.Post, _cfg.PulseUrl.TrimEnd('/') + "/v1/iiko/shift-survey")
            {
                Content = new StringContent(payload.ToString(Formatting.None), Encoding.UTF8, "application/json")
            };
            req.Headers.Add("X-Iiko-Key", _cfg.ApiKey);
            var resp = _http.SendAsync(req).GetAwaiter().GetResult();
            var text = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            if (!resp.IsSuccessStatusCode)
                throw new InvalidOperationException((int)resp.StatusCode + " " + text);
            return JObject.Parse(text);
        }

        private static string DepartmentOf(IUser user)
        {
            var role = (user.GetType().GetProperty("Role")?.GetValue(user)?.ToString()
                        ?? user.Name
                        ?? "").ToLowerInvariant();
            if (role.Contains("кух") || role.Contains("повар") || role.Contains("chef") || role.Contains("kitchen"))
                return "kitchen";
            return "hall";
        }
    }

    internal sealed class PluginConfig
    {
        public string PulseUrl { get; set; }
        public string ApiKey { get; set; }
        public string OrganizationId { get; set; }
        public string ServicePin { get; set; }

        public static PluginConfig Load()
        {
            var path = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "plugin.json");
            if (!File.Exists(path))
                throw new FileNotFoundException("Рядом с DLL нужен plugin.json", path);
            return JsonConvert.DeserializeObject<PluginConfig>(File.ReadAllText(path));
        }
    }

    internal sealed class SurveyWindow : Window
    {
        public int Rating { get; private set; }
        public string Blocker { get; private set; } = "ok";

        public SurveyWindow()
        {
            Title = "Как прошла смена";
            Width = 520;
            Height = 420;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            var root = new StackPanel { Margin = new Thickness(16) };
            root.Children.Add(new TextBlock { Text = "Оценка смены", FontSize = 18, Margin = new Thickness(0, 0, 0, 8) });
            var stars = new WrapPanel();
            for (var i = 1; i <= 5; i++)
            {
                var n = i;
                var b = new Button { Content = n.ToString(), Width = 56, Height = 56, Margin = new Thickness(4), FontSize = 20 };
                b.Click += (_, __) => Rating = n;
                stars.Children.Add(b);
            }
            root.Children.Add(stars);
            root.Children.Add(new TextBlock { Text = "Что мешало работать", FontSize = 18, Margin = new Thickness(0, 16, 0, 8) });
            foreach (var pair in new[]
                     {
                         ("team", "Команда"),
                         ("kitchen", "Кухня"),
                         ("guests", "Гости"),
                         ("processes", "Процессы"),
                         ("self", "Моё состояние"),
                         ("ok", "Нигде — всё прошло хорошо")
                     })
            {
                var code = pair.Item1;
                var btn = new Button { Content = pair.Item2, Margin = new Thickness(0, 4, 0, 0), Height = 36 };
                btn.Click += (_, __) => Blocker = code;
                root.Children.Add(btn);
            }
            var ready = new Button { Content = "Готово", Height = 44, Margin = new Thickness(0, 16, 0, 0), FontSize = 18 };
            ready.Click += (_, __) =>
            {
                if (Rating < 1)
                {
                    MessageBox.Show("Сначала оценка смены.");
                    return;
                }
                DialogResult = true;
                Close();
            };
            root.Children.Add(ready);
            Content = new ScrollViewer { Content = root };
        }
    }
}
