package local.planomp;

import com.intellij.openapi.project.Project;
import com.intellij.openapi.startup.ProjectActivity;
import kotlin.Unit;
import kotlin.coroutines.Continuation;
import org.jetbrains.annotations.NotNull;

public final class ImplementerStartup implements ProjectActivity {
    @Override
    public Object execute(@NotNull Project project, @NotNull Continuation<? super Unit> continuation) {
        project.getService(ImplementerService.class).start();
        return Unit.INSTANCE;
    }
}
