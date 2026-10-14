"""Where AI apps registered before 0.8 start mcp_server: it now lives in the graph_mind
package. graph-mind-install registers the new place; this file goes in 0.9."""
import sys

from graph_mind.mcp_server import main

sys.exit(main())
