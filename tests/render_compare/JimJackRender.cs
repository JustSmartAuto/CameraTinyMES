using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Cognex.InSight.Remoting.Serialization;
using Cognex.InSight.Web;
using Cognex.InSight.Web.Controls;
using Newtonsoft.Json.Linq;

// 使用 CameraHelperJimJack 的真实取图/绘制链路：
//   CvsInSight.Connect -> ManualAcquire -> GetMainImage/GetGraphicsAsync
//   -> Bitmap(viewport/2) -> DrawImage -> GraphicsHelper.DrawGraphics
// 同时把原图、图形 JSON、SVG、视口信息落盘，供 CameraTinyMES 用同一帧渲染对比。
internal static class Program
{
    private static int Main(string[] args)
    {
        try
        {
            return Run(args).GetAwaiter().GetResult();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("FAILED: " + ex);
            return 1;
        }
    }

    private static async Task<int> Run(string[] args)
    {
        string host = "192.168.10.55";
        int port = 80;
        int count = 2;
        string cell = "A0";
        string outRoot = Path.Combine(Path.GetTempPath(), "ctm_jimjack");

        for (int i = 0; i < args.Length - 1; i++)
        {
            switch (args[i])
            {
                case "--host": host = args[++i]; break;
                case "--port": port = int.Parse(args[++i]); break;
                case "--count": count = int.Parse(args[++i]); break;
                case "--cell": cell = args[++i]; break;
                case "--out": outRoot = args[++i]; break;
            }
        }

        Directory.CreateDirectory(outRoot);

        HmiSessionInfo session = new HmiSessionInfo();
        session.SheetName = "Inspection";
        session.CellNames = new string[] { cell + ":Z599" };
        session.EnableQueuedResults = true;
        session.IncludeCustomView = true;

        CvsInSight insight = new CvsInSight();
        ManualResetEventSlim resultReady = new ManualResetEventSlim(false);
        insight.ResultsChanged += delegate { resultReady.Set(); };

        string address = host + ":" + port;
        Console.WriteLine("connect " + address);
        await insight.Connect(address, "admin", "", session);
        resultReady.Reset();

        try
        {
            for (int i = 1; i <= count; i++)
            {
                await insight.SendReady();
                resultReady.Reset();
                await insight.ManualAcquire();
                if (!resultReady.Wait(30000))
                {
                    Console.Error.WriteLine("等待结果超时: " + i);
                    return 2;
                }

                string frameDir = Path.Combine(outRoot, "frame_" + i);
                Directory.CreateDirectory(frameDir);

                JToken results = insight.Results;
                File.WriteAllText(Path.Combine(frameDir, "result.json"),
                    results == null ? "null" : results.ToString(), Encoding.UTF8);

                int viewWidth = insight.ViewPort.Width;
                int viewHeight = insight.ViewPort.Height;
                int offsetY = insight.ImageOffsetY;
                bool usesXY = insight.UsesXYCoordinates;

                using (Image source = await insight.GetMainImage())
                {
                    using (Bitmap sourceCopy = new Bitmap(source.Width, source.Height,
                        PixelFormat.Format24bppRgb))
                    using (Graphics cg = Graphics.FromImage(sourceCopy))
                    {
                        cg.DrawImage(source, new Rectangle(0, 0, sourceCopy.Width, sourceCopy.Height));
                        sourceCopy.Save(Path.Combine(frameDir, "image.bmp"), ImageFormat.Bmp);
                    }
                    CvsCogShape[] shapes = await insight.GetGraphicsAsync();

                    int imageWidth = Math.Max(viewWidth / 2, 1);
                    int imageHeight = Math.Max(viewHeight / 2, 1);
                    float scaleFactor = 0.5f;
                    int startY = 0 - (int)(offsetY * scaleFactor);
                    DisplayContext dc = new DisplayContext(usesXY, scaleFactor, 0, 0,
                        new Rectangle(0, 0, imageWidth, imageHeight));

                    using (Bitmap img = new Bitmap(imageWidth, imageHeight))
                    {
                        using (Graphics g = Graphics.FromImage(img))
                        {
                            g.DrawImage(source,
                                new Rectangle(0, 0, imageWidth, imageHeight),
                                new Rectangle(0, 0, source.Width, source.Height),
                                GraphicsUnit.Pixel);
                            GraphicsHelper.DrawGraphics(g, shapes, dc, true);
                        }
                        img.Save(Path.Combine(frameDir, "jimjack_" + i + ".png"), ImageFormat.Png);
                    }

                    Console.WriteLine(string.Format(
                        "frame {0}: {1}x{2}, shapes={3}, offsetY={4}, usesXY={5}, startY={6}",
                        i, imageWidth, imageHeight, shapes.Length, offsetY, usesXY, startY));
                }

                // 原始图形 JSON 与 SVG 层（会话内下载，否则 404）
                string graphicsUrl = null;
                string svgUrl = null;
                CvsCogGraphicsLayer layer = insight.GetGraphicsLayer();
                if (layer != null && !string.IsNullOrEmpty(layer.Url))
                {
                    graphicsUrl = "http://" + address + layer.Url;
                }
                if (results != null)
                {
                    JToken layers = results.SelectToken("acqImageView.layers");
                    if (layers != null)
                    {
                        foreach (JToken item in layers)
                        {
                            if ((string)item["$type"] == "SVGLayer" && item["url"] != null)
                            {
                                svgUrl = "http://" + address + (string)item["url"];
                                break;
                            }
                        }
                    }
                }

                using (WebClient web = new WebClient())
                {
                    // 相机响应不带 charset 时 WebClient 会按系统 ANSI(GBK) 解码，中文变乱码
                    web.Encoding = Encoding.UTF8;
                    if (graphicsUrl != null)
                    {
                        File.WriteAllText(Path.Combine(frameDir, "graphics.json"),
                            web.DownloadString(graphicsUrl), Encoding.UTF8);
                    }
                    if (svgUrl != null)
                    {
                        File.WriteAllText(Path.Combine(frameDir, "layer.svg"),
                            web.DownloadString(svgUrl), Encoding.UTF8);
                    }
                }

                string meta = string.Format(
                    "{{\"host\":\"{0}\",\"port\":{1},\"viewport_width\":{2},\"viewport_height\":{3}," +
                    "\"image_width\":{4},\"image_height\":{5},\"image_offset_y\":{6},\"uses_xy_coordinates\":{7}}}",
                    host, port, viewWidth, viewHeight, viewWidth, viewHeight, offsetY,
                    usesXY ? "true" : "false");
                File.WriteAllText(Path.Combine(frameDir, "meta.json"), meta, Encoding.UTF8);

                await insight.SendReady();
            }
        }
        catch (Exception)
        {
            try { insight.Disconnect().Wait(3000); } catch { }
            throw;
        }

        await insight.Disconnect();
        Console.WriteLine("done: " + outRoot);
        return 0;
    }
}
