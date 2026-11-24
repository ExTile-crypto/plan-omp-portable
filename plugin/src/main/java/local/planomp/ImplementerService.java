package local.planomp;

import com.intellij.openapi.Disposable;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.components.Service;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.util.Disposer;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowManager;
import com.intellij.ui.components.JBLabel;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import com.intellij.util.concurrency.AppExecutorUtil;
import com.intellij.util.ui.UIUtil;
import org.cef.browser.CefBrowser;
import org.cef.browser.CefFrame;
import org.cef.handler.CefLoadHandlerAdapter;

import javax.swing.JComponent;
import javax.swing.SwingConstants;
import java.awt.Color;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Arrays;
import java.util.Base64;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

@Service(Service.Level.PROJECT)
public final class ImplementerService implements Disposable {
    static final String TOOL_WINDOW = "Implementer";
    private static final Logger LOG = Logger.getInstance(ImplementerService.class);
    private static final Path RUNS = Path.of(System.getProperty("user.home"), ".cache", "omp-exec", "runs");
    private static final Pattern REPO = Pattern.compile("\"repo\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");
    private static final Pattern TIME = Pattern.compile("\"t\"\\s*:\\s*([0-9.]+)");
    private static final Pattern PID = Pattern.compile("\"pid\"\\s*:\\s*(\\d+)");
    private static final int MAX_READ = 1_048_576;
    private static final long SCAN_MS = 1500;

    private final Project project;
    private final long openedAt = System.currentTimeMillis();
    private final Map<String, Boolean> known = new HashMap<>();
    private final long[] progressOffset = {0};
    private final long[] detailOffset = {0};
    private ScheduledFuture<?> task;
    private JBCefBrowser browser;
    private boolean ready;
    private Path run;
    private long pid;
    private long lastScan;
    private Boolean lastAlive;

    public ImplementerService(Project project) {
        this.project = project;
    }

    synchronized void start() {
        if (task == null && !project.isDisposed()) {
            task = AppExecutorUtil.getAppScheduledExecutorService().scheduleWithFixedDelay(this::tick, 500, 350, TimeUnit.MILLISECONDS);
        }
    }

    JComponent createView(ToolWindow window) {
        start();
        if (!JBCefApp.isSupported()) {
            return new JBLabel("The embedded browser is unavailable in this IDE.", SwingConstants.CENTER);
        }
        JBCefBrowser view = JBCefBrowser.createBuilder().build();
        Disposer.register(window.getContentManager(), view);
        Disposer.register(view, () -> detach(view));
        view.getJBCefClient().addLoadHandler(new CefLoadHandlerAdapter() {
            @Override
            public void onLoadEnd(CefBrowser cef, CefFrame frame, int status) {
                if (frame.isMain()) {
                    AppExecutorUtil.getAppExecutorService().execute(() -> pageReady(view));
                }
            }
        }, view.getCefBrowser());
        synchronized (this) {
            browser = view;
            ready = false;
        }
        view.loadHTML(page());
        return view.getComponent();
    }

    private synchronized void detach(JBCefBrowser view) {
        if (browser == view) {
            browser = null;
            ready = false;
        }
    }

    private synchronized void pageReady(JBCefBrowser view) {
        if (browser != view) {
            return;
        }
        ready = true;
        progressOffset[0] = 0;
        detailOffset[0] = 0;
        lastAlive = null;
        script(theme() + "implementer.reset();");
    }

    private synchronized void tick() {
        try {
            if (project.isDisposed()) {
                return;
            }
            long now = System.currentTimeMillis();
            if (run == null || now - lastScan >= SCAN_MS) {
                lastScan = now;
                scan();
            }
            if (ready && run != null) {
                send(run.resolve("progress.jsonl"), progressOffset);
                send(run.resolve("details.jsonl"), detailOffset);
                boolean alive = pid > 0 && ProcessHandle.of(pid).map(ProcessHandle::isAlive).orElse(false);
                if (lastAlive == null || lastAlive != alive) {
                    lastAlive = alive;
                    script("implementer.setAlive(" + alive + ");");
                }
            }
        } catch (IOException | RuntimeException error) {
            LOG.debug("Implementer update failed", error);
        }
    }

    private void scan() throws IOException {
        if (!Files.isDirectory(RUNS)) {
            return;
        }
        List<Path> dirs;
        try (Stream<Path> stream = Files.list(RUNS)) {
            dirs = stream.filter(Files::isDirectory)
                    .sorted(Comparator.comparing((Path path) -> path.getFileName().toString()).reversed())
                    .limit(40)
                    .toList();
        }
        for (Path dir : dirs) {
            String first = firstLine(dir.resolve("progress.jsonl"));
            if (first == null || !belongsHere(dir, first)) {
                continue;
            }
            if (!dir.equals(run)) {
                switchRun(dir, first);
            }
            return;
        }
    }

    private boolean belongsHere(Path dir, String first) {
        return known.computeIfAbsent(dir.getFileName().toString(), name -> {
            Matcher matcher = REPO.matcher(first);
            return matcher.find() && samePath(matcher.group(1).replace("\\\"", "\"").replace("\\\\", "\\"), project.getBasePath());
        });
    }

    private static boolean samePath(String left, String right) {
        if (left == null || right == null) {
            return false;
        }
        try {
            return Path.of(left).toRealPath().equals(Path.of(right).toRealPath());
        } catch (IOException | RuntimeException error) {
            return false;
        }
    }

    private void switchRun(Path dir, String first) {
        run = dir;
        progressOffset[0] = 0;
        detailOffset[0] = 0;
        lastAlive = null;
        Matcher process = PID.matcher(first);
        pid = process.find() ? Long.parseLong(process.group(1)) : 0;
        if (ready) {
            script("implementer.reset();");
        }
        Matcher time = TIME.matcher(first);
        if (time.find() && Double.parseDouble(time.group(1)) * 1000 >= openedAt - 5000) {
            showWindow();
        }
    }

    private void showWindow() {
        ApplicationManager.getApplication().invokeLater(() -> {
            if (project.isDisposed()) {
                return;
            }
            ToolWindow window = ToolWindowManager.getInstance(project).getToolWindow(TOOL_WINDOW);
            if (window != null && !window.isVisible()) {
                window.show();
            }
        });
    }

    private void send(Path file, long[] offset) throws IOException {
        if (!Files.exists(file)) {
            return;
        }
        try (RandomAccessFile input = new RandomAccessFile(file.toFile(), "r")) {
            long length = input.length();
            if (length <= offset[0]) {
                return;
            }
            int size = (int) Math.min(length - offset[0], MAX_READ);
            byte[] bytes = new byte[size];
            input.seek(offset[0]);
            input.readFully(bytes);
            int last = size - 1;
            while (last >= 0 && bytes[last] != '\n') {
                last--;
            }
            if (last < 0) {
                if (size == MAX_READ) {
                    offset[0] += size;
                }
                return;
            }
            offset[0] += last + 1;
            String encoded = Base64.getEncoder().encodeToString(Arrays.copyOf(bytes, last + 1));
            script("implementer.addBatch('" + encoded + "');");
        }
    }

    private void script(String code) {
        if (browser != null) {
            browser.getCefBrowser().executeJavaScript("if(window.implementer){" + code + "}", "", 0);
        }
    }

    private static String firstLine(Path file) {
        try (BufferedReader reader = Files.newBufferedReader(file, StandardCharsets.UTF_8)) {
            return reader.readLine();
        } catch (IOException | RuntimeException error) {
            return null;
        }
    }

    private static String theme() {
        String font = UIUtil.getLabelFont().getFamily().replaceAll("[^\\w -]", "");
        return "implementer.theme({bg:'" + hex(UIUtil.getPanelBackground()) + "',fg:'" + hex(UIUtil.getLabelForeground()) + "',font:'" + font + "'});";
    }

    private static String hex(Color color) {
        return String.format("#%02x%02x%02x", color.getRed(), color.getGreen(), color.getBlue());
    }

    private static String page() {
        try (InputStream input = ImplementerService.class.getResourceAsStream("/implementer/index.html")) {
            if (input == null) {
                return "<!doctype html><title>Implementer</title><p>The panel page is missing.</p>";
            }
            return new String(input.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException error) {
            return "<!doctype html><title>Implementer</title><p>The panel page could not be read.</p>";
        }
    }

    @Override
    public synchronized void dispose() {
        if (task != null) {
            task.cancel(false);
            task = null;
        }
        browser = null;
        ready = false;
    }
}
