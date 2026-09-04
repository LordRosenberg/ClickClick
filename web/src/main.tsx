import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

import { TaskListView } from "@/views/TaskListView";
import { TaskDetailView } from "@/views/TaskDetailView";
import { DeviceView } from "@/views/DeviceView";
import { SkillsView } from "@/views/SkillsView";
import { RootLayout } from "@/views/RootLayout";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <RootLayout />,
    children: [
      { index: true, element: <TaskListView /> },
      { path: "tasks/:id", element: <TaskDetailView /> },
      { path: "device", element: <DeviceView /> },
      { path: "skills", element: <SkillsView /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>
);
