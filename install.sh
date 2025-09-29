#!/bin/bash

# fcrawl Installation Script

echo "Installing fcrawl CLI tool..."

# Get the directory of this script
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Create a symlink in a directory that's in PATH
INSTALL_DIR="$HOME/.local/bin"
mkdir -p "$INSTALL_DIR"

# Create wrapper script
cat > "$INSTALL_DIR/fcrawl" << EOF
#!/bin/bash
python3 "$SCRIPT_DIR/fcrawl.py" "\$@"
EOF

chmod +x "$INSTALL_DIR/fcrawl"

echo "✓ fcrawl installed to $INSTALL_DIR/fcrawl"

# Check if .local/bin is in PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo ""
    echo "⚠️  Note: $HOME/.local/bin is not in your PATH"
    echo "Add it to your shell config with:"
    echo "  fish_add_path \$HOME/.local/bin  # for fish shell"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"  # for bash/zsh"
fi

echo ""
echo "Installation complete! Try running: fcrawl --help"