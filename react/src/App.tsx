import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster as Sonner } from "openbase-react-ui";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ProjectRoutesWithChat } from "./components/ProjectRoutesWithChat";
import { AgentChatProvider } from "./contexts/AgentChatContext";
import { ProjectProvider } from "./contexts/ProjectContext";
import AdminPage from "./pages/AdminPage";
import CommandsPage from "./pages/CommandsPage";
import EndpointPage from "./pages/EndpointPage";
import EndpointsPage from "./pages/EndpointsPage";
import ModelsPage from "./pages/ModelsPage";
import NotFound from "./pages/NotFound";
import OpenProject from "./pages/OpenProject";
import ProjectHome from "./pages/ProjectHome";
import ProjectSettingsPage from "./pages/ProjectSettingsPage";
import SerializersPage from "./pages/SerializersPage";
import TasksPage from "./pages/TasksPage";

const queryClient = new QueryClient();

const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <Toaster />
      <Sonner />
      <BrowserRouter>
        <Routes>
          <Route
            path="/"
            element={<Navigate to="/projects/local/" replace />}
          />
          <Route path="/open-project/:projectId/*" element={<OpenProject />} />
          <Route
            path="/projects/:projectId/*"
            element={
              <ProjectProvider>
                <AgentChatProvider>
                  <ProjectRoutesWithChat>
                    <Route index element={<ProjectHome />} />
                    <Route path="settings" element={<ProjectSettingsPage />} />
                    <Route path=":appName/models" element={<ModelsPage />} />
                    <Route
                      path=":appName/models/relationships"
                      element={<ModelsPage />}
                    />
                    <Route
                      path=":appName/endpoints"
                      element={<EndpointsPage />}
                    />
                    <Route
                      path=":appName/endpoint"
                      element={<EndpointPage />}
                    />
                    <Route
                      path=":appName/serializers"
                      element={<SerializersPage />}
                    />
                    <Route path=":appName/tasks" element={<TasksPage />} />
                    <Route
                      path=":appName/commands"
                      element={<CommandsPage />}
                    />
                    <Route
                      path="admin/:appName/:modelName"
                      element={<AdminPage />}
                    />
                  </ProjectRoutesWithChat>
                </AgentChatProvider>
              </ProjectProvider>
            }
          />
          {/* ADD ALL CUSTOM ROUTES ABOVE THE CATCH-ALL "*" ROUTE */}
          <Route path="*" element={<NotFound />} />
        </Routes>
      </BrowserRouter>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
