import { Children, isValidElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { MermaidDiagram } from "./MermaidDiagram.js";

interface RichMarkdownProps {
  readonly children: string;
}

const components: Components = {
  a: ({ children, ...props }) => <a {...props} target="_blank" rel="noreferrer">{children}</a>,
  pre: ({ children, ...props }) => {
    const child = Children.count(children) === 1 ? Children.only(children) : null;
    if (isValidElement<{ readonly className?: string; readonly children?: ReactNode }>(child)
      && /(?:^|\s)language-mermaid(?:\s|$)/.test(child.props.className ?? "")) {
      return <MermaidDiagram source={textContent(child.props.children).trim()} />;
    }
    return <pre {...props}>{children}</pre>;
  },
};

export function RichMarkdown({ children }: RichMarkdownProps): React.JSX.Element {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{children}</ReactMarkdown>;
}

function textContent(value: ReactNode): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(textContent).join("");
  if (isValidElement<{ readonly children?: ReactNode }>(value)) return textContent(value.props.children);
  return "";
}
