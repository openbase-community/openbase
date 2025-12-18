import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  AdminPage,
  AgentChatProvider,
  CommandsPage,
  EndpointPage,
  EndpointsPage,
  ModelsPage,
  ProjectHome,
  ProjectProvider,
  ProjectRoutesWithChat,
  ProjectSettingsPage,
  SerializersPage,
  TasksPage,
} from "openbase-react-ui";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import NotFound from "./pages/NotFound";
import OpenProject from "./pages/OpenProject";

const queryClient = new QueryClient();

const App = () => (
  <QueryClientProvider client={queryClient}>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate to="/projects/local/" replace />} />
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
                  <Route path=":appName/endpoint" element={<EndpointPage />} />
                  <Route
                    path=":appName/serializers"
                    element={<SerializersPage />}
                  />
                  <Route path=":appName/tasks" element={<TasksPage />} />
                  <Route path=":appName/commands" element={<CommandsPage />} />
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
  </QueryClientProvider>
);

export default App;
