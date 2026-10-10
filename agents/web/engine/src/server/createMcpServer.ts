import { McpServer } from "@modelcontextprotocol/server";
import { registerAllTools } from "../tools/index.js";
import { installBridgeGate } from '../bridge/gate.js';

export function buildMcpServer(): McpServer {
  const server = new McpServer({
    name: "Zagent Web",
    version: "2.2.10-zagent.1",
  });

  installBridgeGate(server);
  registerAllTools(server);
  return server;
}
