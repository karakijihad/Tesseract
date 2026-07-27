import type { EntityController } from './EntityController';

let _controller: EntityController | null = null;

export function setController(c: EntityController | null): void {
  _controller = c;
}

export function getController(): EntityController | null {
  return _controller;
}
