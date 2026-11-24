package local.planomp;

import com.intellij.openapi.project.DumbAware;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowFactory;
import com.intellij.ui.content.Content;
import com.intellij.ui.content.ContentFactory;
import org.jetbrains.annotations.NotNull;

public final class ImplementerToolWindowFactory implements ToolWindowFactory, DumbAware {
    @Override
    public void createToolWindowContent(@NotNull Project project, @NotNull ToolWindow toolWindow) {
        ImplementerService service = project.getService(ImplementerService.class);
        Content content = ContentFactory.getInstance().createContent(service.createView(toolWindow), "", false);
        content.setCloseable(false);
        toolWindow.getContentManager().addContent(content);
    }
}
