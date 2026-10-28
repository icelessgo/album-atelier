// A response belongs to one unchanged editing context. Navigation and newer
// requests invalidate it even if the user returns to the same spread later.
export class LayoutTarget {
  constructor() { this.generation = 0; }
  invalidate() { this.generation++; }
  capture(project, active) {
    return {generation: ++this.generation, projectId: project.id, active,
      signature: JSON.stringify([project.spreads, project.design_format, project.photo_ids])};
  }
  matches(target, project, active) {
    return Boolean(project && target.generation === this.generation && project.id === target.projectId &&
      target.active === active && target.signature === JSON.stringify([project.spreads, project.design_format, project.photo_ids]));
  }
}
