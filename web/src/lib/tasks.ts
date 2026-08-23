import type { TaskResource } from "@/types/api";

export function isTaskBlocked(resource: TaskResource, completedIds: Set<string>) {
  return resource.task.status === "pending"
    && resource.task.blockedBy.some((id) => !completedIds.has(id));
}
