import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** The Aceternity/shadcn class helper: conditional classes with conflict resolution. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
