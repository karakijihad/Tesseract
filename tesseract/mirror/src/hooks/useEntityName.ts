import { useIdentityStore } from '../stores/identity';

/** The neutral noun that stands in before `/api/identity` has answered.
 * Not a name: a placeholder name would be indistinguishable from a real
 * one, so a rename that failed to take would read as working. */
export const ENTITY_FALLBACK = 'the assistant';

/** The operator-settable agent name for use inside a sentence.
 *
 * The name lives in `mirror.yaml::identity.name` and changes at runtime
 * through `POST /api/identity`, so no rendered string may hardcode one.
 * Prose reads badly with a bare gap, hence the noun fallback; a slot that
 * renders the name alone (a header, a title) should read the store
 * directly and render nothing until it loads.
 */
export function useEntityName(): string {
  return useIdentityStore((s) => s.name) || ENTITY_FALLBACK;
}
