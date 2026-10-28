// Copyright (c) 2026 icrafti. SPDX-License-Identifier: LicenseRef-Icrafti-Attribution-1.0
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace PortableAudioConverter
{
    internal static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            if (args.Length > 0)
            {
                try
                {
                    Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true });
                    if (args.Length == 1 && args[0] == "--self-test")
                    {
                        if (Engine.Quote("C:\\test path\\") != "\"C:\\test path\\\\\"") throw new Exception("quote");
                        if (Engine.CodecArguments("mp3", 192).IndexOf("libmp3lame") < 0) throw new Exception("codec");
                        Console.WriteLine("{\"ok\":true}");
                        return 0;
                    }
                    if (args.Length != 6 || args[0] != "--convert")
                        throw new ArgumentException("Usage: --convert INPUT OUTPUT_DIRECTORY FORMAT BITRATE CANCEL_AFTER_MS (0 = no cancellation)");
                    using (var cancel = new CancellationTokenSource())
                    using (var engine = new Engine())
                    {
                        int delay = int.Parse(args[5], CultureInfo.InvariantCulture);
                        if (delay > 0) cancel.CancelAfter(delay);
                        string output = engine.Convert(args[1], args[2], args[3], int.Parse(args[4], CultureInfo.InvariantCulture), cancel.Token, delegate { });
                        Console.WriteLine("{\"ok\":true,\"output\":\"" + Json(output) + "\"}");
                        return 0;
                    }
                }
                catch (OperationCanceledException) { Console.WriteLine("{\"ok\":false,\"cancelled\":true}"); return 2; }
                catch (Exception ex) { Console.WriteLine("{\"ok\":false,\"error\":\"" + Json(ex.Message) + "\"}"); return 1; }
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new MainWindow());
            return 0;
        }

        static string Json(string value)
        {
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t");
        }
    }

    internal sealed class Engine : IDisposable
    {
        string temporaryDirectory;
        string executable;

        internal static string Quote(string value)
        {
            // CommandLineToArgvW-compatible escaping, including a trailing backslash.
            return "\"" + Regex.Replace(value, "(\\\\*)\"", "$1$1\\\"").TrimEnd('\\')
                + new string('\\', (value.Length - value.TrimEnd('\\').Length) * 2) + "\"";
        }

        internal static string CodecArguments(string format, int bitrate)
        {
            if (bitrate != 96 && bitrate != 128 && bitrate != 192 && bitrate != 256 && bitrate != 320)
                throw new ArgumentException("Unsupported quality setting.");
            switch (format.ToLowerInvariant())
            {
                case "mp3": return "-c:a libmp3lame -b:a " + bitrate + "k";
                case "wav": return "-c:a pcm_s16le";
                case "flac": return "-c:a flac -compression_level 5";
                case "m4a": return "-c:a aac -b:a " + bitrate + "k -movflags +faststart";
                case "ogg": return "-c:a libvorbis -q:a " + (bitrate <= 96 ? "2" : bitrate <= 128 ? "4" : bitrate <= 192 ? "6" : bitrate <= 256 ? "8" : "10");
                default: throw new ArgumentException("Choose MP3, WAV, FLAC, M4A or OGG.");
            }
        }

        void Prepare(CancellationToken token)
        {
            if (executable != null) return;
            token.ThrowIfCancellationRequested();
            temporaryDirectory = Path.Combine(Path.GetTempPath(), "PortableAudioConverter-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(temporaryDirectory);
            executable = Path.Combine(temporaryDirectory, "ffmpeg.exe");
            using (Stream resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("ffmpeg.gz"))
            {
                if (resource == null) throw new IOException("The audio engine is missing. Rebuild the executable.");
                using (var packed = new GZipStream(resource, CompressionMode.Decompress))
                using (var destination = File.Create(executable))
                {
                    byte[] buffer = new byte[1024 * 1024];
                    int count;
                    while ((count = packed.Read(buffer, 0, buffer.Length)) > 0)
                    {
                        token.ThrowIfCancellationRequested();
                        destination.Write(buffer, 0, count);
                    }
                }
            }
            token.ThrowIfCancellationRequested();
        }

        internal string Convert(string input, string directory, string format, int bitrate, CancellationToken token, Action<int, string> progress)
        {
            input = Path.GetFullPath(input);
            directory = Path.GetFullPath(directory);
            format = format.ToLowerInvariant();
            string codec = CodecArguments(format, bitrate);
            if (!File.Exists(input)) throw new FileNotFoundException("The input file was not found.", input);
            if (!Directory.Exists(directory)) throw new DirectoryNotFoundException("The selected output folder does not exist.");
            progress(-1, "Preparing the audio engine...");
            Prepare(token);
            progress(-1, "Reading the audio file...");
            string probe;
            Run("-nostdin -hide_banner -protocol_whitelist file,pipe -i " + Quote(input), token, null, out probe);
            if (probe.IndexOf("Audio:", StringComparison.OrdinalIgnoreCase) < 0)
                throw new InvalidDataException("No readable audio track was found. Check that the file is valid and contains audio.");
            Match durationMatch = Regex.Match(probe, @"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)");
            double duration = 0;
            if (durationMatch.Success)
                duration = double.Parse(durationMatch.Groups[1].Value, CultureInfo.InvariantCulture) * 3600
                    + double.Parse(durationMatch.Groups[2].Value, CultureInfo.InvariantCulture) * 60
                    + double.Parse(durationMatch.Groups[3].Value, CultureInfo.InvariantCulture);
            string partial = Path.Combine(directory, ".audioconverter-" + Guid.NewGuid().ToString("N") + "." + format);
            try
            {
                progress(duration > 0 ? 0 : -1, "Converting...");
                string errors;
                int exit = Run("-nostdin -hide_banner -loglevel error -n -protocol_whitelist file,pipe -i " + Quote(input)
                    + " -map 0:a:0 -vn " + codec + " -progress pipe:1 -nostats " + Quote(partial), token,
                    delegate(string line)
                    {
                        long microseconds;
                        if (duration > 0 && line.StartsWith("out_time_us=") && long.TryParse(line.Substring(12), out microseconds))
                        {
                            int percent = (int)Math.Min(99, Math.Max(0, microseconds / 1000000.0 / duration * 100));
                            progress(percent, "Converting... " + percent + "%");
                        }
                    }, out errors);
                token.ThrowIfCancellationRequested();
                if (exit != 0 || !File.Exists(partial) || new FileInfo(partial).Length == 0)
                    throw new IOException("Conversion failed. Check available disk space and the selected format.\r\n\r\n" + errors);
                string stem = Path.GetFileNameWithoutExtension(input);
                for (int number = 0; number < 10000; number++)
                {
                    string output = Path.Combine(directory, stem + (number == 0 ? "" : " (" + number + ")") + "." + format);
                    if (File.Exists(output) || Directory.Exists(output)) continue;
                    try { File.Move(partial, output); progress(100, "Done"); return output; }
                    catch (IOException) { if (!File.Exists(output) && !Directory.Exists(output)) throw; }
                }
                throw new IOException("Too many files share this name. Choose a different folder.");
            }
            finally { try { if (File.Exists(partial)) File.Delete(partial); } catch (IOException) { } }
        }

        int Run(string arguments, CancellationToken token, Action<string> output, out string errors)
        {
            token.ThrowIfCancellationRequested();
            var messages = new StringBuilder();
            using (var process = new Process())
            {
                process.StartInfo = new ProcessStartInfo(executable, arguments) {
                    UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true,
                    RedirectStandardError = true, StandardErrorEncoding = Encoding.UTF8, StandardOutputEncoding = Encoding.UTF8
                };
                process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { if (e.Data != null && output != null) output(e.Data); };
                process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) {
                    if (e.Data != null) lock (messages) { if (messages.Length < 32768) messages.AppendLine(e.Data); }
                };
                process.Start();
                using (token.Register(delegate { try { if (!process.HasExited) process.Kill(); } catch (InvalidOperationException) { } catch (System.ComponentModel.Win32Exception) { } }))
                {
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    process.WaitForExit();
                    token.ThrowIfCancellationRequested();
                    errors = messages.ToString();
                    return process.ExitCode;
                }
            }
        }

        public void Dispose()
        {
            if (temporaryDirectory != null)
                try { Directory.Delete(temporaryDirectory, true); }
                catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
    }

    internal sealed class MainWindow : Form
    {
        readonly TextBox input = new TextBox();
        readonly TextBox destination = new TextBox();
        readonly ComboBox format = new ComboBox();
        readonly ComboBox quality = new ComboBox();
        readonly Button browseInput = new Button();
        readonly Button browseDestination = new Button();
        readonly Button convert = new Button();
        readonly Button cancel = new Button();
        readonly Button open = new Button();
        readonly ProgressBar progress = new ProgressBar();
        readonly Label status = new Label();
        readonly Label hint = new Label();
        CancellationTokenSource cancellation;
        bool closeWhenFinished;
        string lastOutput;

        public MainWindow()
        {
            Text = "Audio Converter";
            Font = new Font("Segoe UI", 10F);
            BackColor = Color.FromArgb(243, 246, 250);
            ForeColor = Color.FromArgb(24, 43, 68);
            ClientSize = new Size(650, 552);
            MinimumSize = new Size(620, 591);
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            AllowDrop = true;

            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(28, 22, 28, 20), ColumnCount = 2, RowCount = 12 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 112));
            float[] heights = { 42, 28, 36, 28, 36, 30, 40, 40, 46, 24, 50, 36 };
            foreach (float height in heights) layout.RowStyles.Add(new RowStyle(SizeType.Absolute, height));
            Controls.Add(layout);
            Add(layout, new Label { Text = "Audio Converter", Font = new Font("Segoe UI", 22F, FontStyle.Bold), Dock = DockStyle.Fill }, 0, true);
            Add(layout, Label("Audio file"), 1, true);
            input.Dock = DockStyle.Fill; input.AccessibleName = "Input audio file";
            layout.Controls.Add(input, 0, 2);
            SetButton(browseInput, "Choose...", delegate { ChooseFile(); }); layout.Controls.Add(browseInput, 1, 2);
            Add(layout, Label("Output folder"), 3, true);
            destination.Dock = DockStyle.Fill; destination.AccessibleName = "Output folder";
            layout.Controls.Add(destination, 0, 4);
            SetButton(browseDestination, "Browse...", delegate {
                using (var dialog = new FolderBrowserDialog { Description = "Choose an output folder", SelectedPath = destination.Text })
                    if (dialog.ShowDialog(this) == DialogResult.OK) destination.Text = dialog.SelectedPath;
            }); layout.Controls.Add(browseDestination, 1, 4);
            Add(layout, Label("Format and quality"), 5, true);
            var options = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = false, Margin = Padding.Empty };
            format.DropDownStyle = quality.DropDownStyle = ComboBoxStyle.DropDownList;
            format.Width = 130; quality.Width = 260;
            format.AccessibleName = "Format"; quality.AccessibleName = "Quality";
            format.Items.AddRange(new object[] { "MP3", "WAV", "FLAC", "M4A", "OGG" });
            quality.Items.AddRange(new object[] { "96 kbps - compact", "128 kbps", "192 kbps - standard", "256 kbps", "320 kbps - maximum" });
            format.SelectedIndex = 0; quality.SelectedIndex = 2;
            options.Controls.Add(format); options.Controls.Add(quality);
            Add(layout, options, 6, true);
            hint.Text = "You can drop an audio file into this window.";
            hint.Dock = DockStyle.Fill; hint.ForeColor = Color.FromArgb(88, 103, 123);
            Add(layout, hint, 7, true);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = false, Margin = Padding.Empty };
            SetButton(convert, "Convert", async delegate { await StartConversion(); });
            convert.Width = 185; convert.BackColor = Color.FromArgb(36, 93, 184); convert.ForeColor = Color.White; convert.FlatStyle = FlatStyle.Flat;
            SetButton(cancel, "Cancel", delegate { CancelConversion(); }); cancel.Width = 110; cancel.Enabled = false;
            actions.Controls.Add(convert); actions.Controls.Add(cancel); Add(layout, actions, 8, true);
            progress.Dock = DockStyle.Fill; progress.Margin = new Padding(3, 3, 3, 6); Add(layout, progress, 9, true);
            status.Text = "Choose a file. Your original recording stays unchanged.";
            status.Dock = DockStyle.Fill; status.AutoEllipsis = true; Add(layout, status, 10, true);
            SetButton(open, "Open folder", delegate {
                if (lastOutput != null) try { Process.Start("explorer.exe", "/select," + Engine.Quote(lastOutput)); }
                catch (Exception ex) { MessageBox.Show(this, ex.Message, "Cannot open folder"); }
            }); open.Width = 155; open.Enabled = false; layout.Controls.Add(open, 0, 11);
            var about = new LinkLabel { Text = "About / icrafti", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleRight };
            about.LinkClicked += delegate { MessageBox.Show(this,
                "Audio Converter 1.1\r\nSoftware by icrafti\r\nhttps://github.com/icrafti/software-collection\r\n\r\nPortable for Windows 10/11 x64, .NET Framework 4.x.\r\nApplication: Icrafti Attribution License 1.0. Retain author credit.\r\nFFmpeg 8.1 (Gyan full build) is a separate engine under GPL v3+.\r\nEngine source: https://www.gyan.dev/ffmpeg/builds/\r\nhttps://ffmpeg.org/\r\nNo internet connection is needed to convert audio.", "About / icrafti", MessageBoxButtons.OK, MessageBoxIcon.Information); };
            layout.Controls.Add(about, 1, 11);
            AcceptButton = convert;
            format.SelectedIndexChanged += delegate { RefreshQuality(); };
            DragEnter += delegate(object sender, DragEventArgs e) { e.Effect = cancellation == null && e.Data.GetDataPresent(DataFormats.FileDrop) ? DragDropEffects.Copy : DragDropEffects.None; };
            DragDrop += delegate(object sender, DragEventArgs e) { var paths = e.Data.GetData(DataFormats.FileDrop) as string[]; if (paths != null && paths.Length > 0 && File.Exists(paths[0])) SetInput(paths[0]); };
            FormClosing += delegate(object sender, FormClosingEventArgs e) { if (cancellation != null) { e.Cancel = true; closeWhenFinished = true; CancelConversion(); } };
        }

        static Label Label(string text) { return new Label { Text = text, Dock = DockStyle.Fill, TextAlign = ContentAlignment.BottomLeft, Padding = new Padding(0, 0, 0, 4) }; }
        static void Add(TableLayoutPanel panel, Control control, int row, bool span) { panel.Controls.Add(control, 0, row); if (span) panel.SetColumnSpan(control, 2); }
        static void SetButton(Button button, string text, EventHandler handler) { button.Text = text; button.Height = 34; button.Dock = DockStyle.None; button.Width = 106; button.Click += handler; }

        void ChooseFile()
        {
            using (var dialog = new OpenFileDialog { Title = "Choose audio", Filter = "Audio files|*.m4a;*.mp3;*.wav;*.flac;*.ogg;*.opus;*.aac;*.wma;*.aiff;*.amr;*.mp4;*.webm|All files|*.*", CheckFileExists = true })
                if (dialog.ShowDialog(this) == DialogResult.OK) SetInput(dialog.FileName);
        }

        void SetInput(string path) { input.Text = path; destination.Text = Path.GetDirectoryName(path); status.Text = "Ready to convert: " + Path.GetFileName(path); }
        void RefreshQuality()
        {
            quality.Enabled = cancellation == null && format.Text != "WAV" && format.Text != "FLAC";
            hint.Text = format.Text == "WAV" ? "WAV: uncompressed 16-bit PCM audio, larger file size."
                : format.Text == "FLAC" ? "FLAC: lossless compression of the decoded source audio."
                : format.Text == "OGG" ? "OGG: variable bitrate; the selected bitrate is approximate."
                : "A higher bitrate cannot restore lost audio quality.";
        }

        void CancelConversion() { if (cancellation != null) { cancellation.Cancel(); cancel.Enabled = false; status.Text = "Stopping..."; } }

        async Task StartConversion()
        {
            if (cancellation != null) return;
            if (!File.Exists(input.Text)) { MessageBox.Show(this, "Choose an existing audio file.", "Audio file", MessageBoxButtons.OK, MessageBoxIcon.Information); return; }
            if (!Directory.Exists(destination.Text)) { MessageBox.Show(this, "Choose an existing output folder.", "Output folder", MessageBoxButtons.OK, MessageBoxIcon.Information); return; }
            string source = input.Text, folder = destination.Text, target = format.Text.ToLowerInvariant();
            int bitrate = new int[] { 96, 128, 192, 256, 320 }[quality.SelectedIndex];
            cancellation = new CancellationTokenSource();
            var token = cancellation.Token;
            foreach (Control control in new Control[] { input, destination, browseInput, browseDestination, format, quality, convert, open }) control.Enabled = false;
            cancel.Enabled = true; progress.Value = 0; lastOutput = null;
            try
            {
                lastOutput = await Task.Run(delegate {
                    using (var engine = new Engine()) return engine.Convert(source, folder, target, bitrate, token,
                        delegate(int percent, string message) {
                            if (!IsDisposed && IsHandleCreated) BeginInvoke((Action)delegate {
                                progress.Style = percent < 0 ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
                                if (percent >= 0) progress.Value = percent;
                                status.Text = message;
                            });
                        });
                });
                status.Text = "Done: " + lastOutput; open.Enabled = true;
            }
            catch (OperationCanceledException) { status.Text = "Conversion cancelled. You can choose another file."; progress.Value = 0; }
            catch (Exception ex) { status.Text = "Could not convert the file."; MessageBox.Show(this, ex.Message, "Conversion error", MessageBoxButtons.OK, MessageBoxIcon.Error); progress.Value = 0; }
            finally
            {
                cancellation.Dispose(); cancellation = null;
                foreach (Control control in new Control[] { input, destination, browseInput, browseDestination, format, convert }) control.Enabled = true;
                cancel.Enabled = false; progress.Style = ProgressBarStyle.Continuous; RefreshQuality();
                if (closeWhenFinished) Close();
            }
        }
    }
}
