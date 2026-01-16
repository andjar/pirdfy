#!/bin/bash
#
# Pirdfy Installation Script
# Bird Feeder Camera Detector for Raspberry Pi 5
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/yourusername/pirdfy/main/install.sh | bash
#
# Or:
#   chmod +x install.sh
#   ./install.sh
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Installation directory
INSTALL_DIR="/opt/pirdfy"
SERVICE_NAME="pirdfy"
SERVICE_USER="pirdfy"
SERVICE_GROUP="pirdfy"
REPO_URL="https://github.com/andjar/pirdfy.git"

# Print functions
print_header() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}  🐦 Pirdfy - Bird Feeder Camera Detector${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
}

print_step() {
    echo -e "${GREEN}▶${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✖${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

# Check if running as root
check_root() {
    if [[ $EUID -ne 0 ]]; then
        print_error "This script must be run as root (use sudo)"
        exit 1
    fi
}

# Create dedicated service user
create_service_user() {
    print_step "Creating service user..."
    
    # Create pirdfy group if it doesn't exist
    if ! getent group "$SERVICE_GROUP" > /dev/null 2>&1; then
        groupadd --system "$SERVICE_GROUP"
        print_info "Created group: $SERVICE_GROUP"
    else
        print_info "Group $SERVICE_GROUP already exists"
    fi
    
    # Build list of groups that exist on this system
    EXTRA_GROUPS="video"
    for grp in gpio i2c spi render; do
        if getent group "$grp" > /dev/null 2>&1; then
            EXTRA_GROUPS="$EXTRA_GROUPS,$grp"
        fi
    done
    
    # Create pirdfy user if it doesn't exist
    if ! id "$SERVICE_USER" > /dev/null 2>&1; then
        useradd --system \
            --gid "$SERVICE_GROUP" \
            --groups "$EXTRA_GROUPS" \
            --home-dir "$INSTALL_DIR" \
            --no-create-home \
            --shell /usr/sbin/nologin \
            "$SERVICE_USER"
        print_info "Created user: $SERVICE_USER (groups: $EXTRA_GROUPS)"
    else
        print_info "User $SERVICE_USER already exists"
        # Ensure user is in required groups
        usermod -aG "$EXTRA_GROUPS" "$SERVICE_USER" 2>/dev/null || true
    fi
    
    # Add user to video group for camera access (ensure it's there)
    usermod -aG video "$SERVICE_USER" 2>/dev/null || true
    
    print_success "Service user configured"
}

# Check system requirements
check_requirements() {
    print_step "Checking system requirements..."
    
    # Check if Raspberry Pi
    if ! grep -q "Raspberry Pi" /proc/cpuinfo 2>/dev/null; then
        print_warning "This doesn't appear to be a Raspberry Pi. Continuing anyway..."
    fi
    
    # Check Python version
    if command -v python3 &> /dev/null; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
        print_info "Python version: $PYTHON_VERSION"
        
        # Check if Python 3.9+
        if python3 -c 'import sys; exit(0 if sys.version_info >= (3, 9) else 1)'; then
            print_success "Python version OK"
        else
            print_error "Python 3.9 or higher is required"
            exit 1
        fi
    else
        print_error "Python 3 is not installed"
        exit 1
    fi
    
    # Check available memory
    TOTAL_MEM=$(free -m | awk '/^Mem:/{print $2}')
    if [[ $TOTAL_MEM -lt 2048 ]]; then
        print_warning "System has less than 2GB RAM. Performance may be limited."
    fi
    
    # Check available disk space
    AVAIL_SPACE=$(df -BG /opt | awk 'NR==2{print $4}' | sed 's/G//')
    if [[ $AVAIL_SPACE -lt 5 ]]; then
        print_warning "Less than 5GB free space available. Consider freeing up disk space."
    fi
}

# Install system dependencies
install_dependencies() {
    print_step "Installing system dependencies..."
    
    apt-get update
    
    # Core build tools and libraries
    apt-get install -y \
        python3-pip \
        python3-venv \
        python3-dev \
        python3-numpy \
        build-essential \
        cmake \
        git \
        curl \
        wget
    
    # Try to install python3-opencv (may not be available on all distros)
    apt-get install -y python3-opencv 2>/dev/null || print_warning "python3-opencv not available, will install via pip"
    
    # Camera libraries (libcamera for Raspberry Pi Camera)
    # These may have different names on Ubuntu vs Raspberry Pi OS
    apt-get install -y libcap-dev
    apt-get install -y libcamera-dev 2>/dev/null || print_warning "libcamera-dev not available"
    apt-get install -y libcamera-apps 2>/dev/null || true
    
    # Image processing libraries
    apt-get install -y \
        libjpeg-dev \
        libpng-dev \
        libtiff-dev \
        libwebp-dev
    
    # Math/ML libraries
    # Note: libatlas-base-dev has been removed from Debian trixie and newer Ubuntu
    # See: https://github.com/numpy/numpy/issues/29108
    # Using libopenblas-dev as the recommended alternative
    apt-get install -y libopenblas-dev || true
    apt-get install -y libhdf5-dev || true
    apt-get install -y liblapack-dev || true
    
    # Video encoding
    apt-get install -y \
        ffmpeg \
        v4l-utils
    
    print_success "System dependencies installed"
}

# Install picamera2 and camera dependencies
install_picamera2_deps() {
    print_step "Installing Raspberry Pi Camera dependencies..."
    
    # Detect if we're on Ubuntu or Raspberry Pi OS
    if grep -qi "ubuntu" /etc/os-release 2>/dev/null; then
        print_info "Detected Ubuntu - installing camera packages..."
        
        # On Ubuntu, packages may have different names or need PPA
        apt-get install -y python3-libcamera 2>/dev/null || true
        apt-get install -y python3-kms++ 2>/dev/null || true
        apt-get install -y python3-prctl 2>/dev/null || true
        apt-get install -y python3-pil 2>/dev/null || true
        
        # Try to install picamera2 - may need to be installed via pip on Ubuntu
        if ! apt-get install -y python3-picamera2 2>/dev/null; then
            print_warning "python3-picamera2 not available via apt, will try pip later"
        fi
        
        # Camera tools
        apt-get install -y libcamera-tools 2>/dev/null || true
        apt-get install -y libcamera-apps 2>/dev/null || true
        
    else
        print_info "Detected Raspberry Pi OS - installing camera packages..."
        
        # Core picamera2 and libcamera packages (Raspberry Pi OS)
        apt-get install -y \
            python3-picamera2 \
            python3-libcamera \
            python3-kms++ \
            python3-prctl \
            python3-pil \
            2>/dev/null || print_warning "Some camera packages not available"
        
        # Additional libcamera tools
        apt-get install -y rpicam-apps 2>/dev/null || \
            apt-get install -y libcamera-apps 2>/dev/null || \
            apt-get install -y libcamera-tools 2>/dev/null || true
    fi
    
    # PyQt5 for picamera2 preview (optional)
    apt-get install -y python3-pyqt5 2>/dev/null || true
    
    # Enable camera interface via raspi-config if available
    if command -v raspi-config &> /dev/null; then
        print_info "Configuring camera interface..."
        raspi-config nonint do_camera 0 2>/dev/null || true
        raspi-config nonint do_legacy 1 2>/dev/null || true
    fi
    
    # Add service user to video group for camera access
    usermod -aG video "$SERVICE_USER" 2>/dev/null || true
    
    # Also add current user if exists (ubuntu, pi, etc.)
    for user in pi ubuntu; do
        if id "$user" &>/dev/null; then
            usermod -aG video "$user" 2>/dev/null || true
        fi
    done
    
    # Test if camera is detected
    print_info "Testing camera detection..."
    if command -v rpicam-hello &> /dev/null; then
        rpicam-hello --list-cameras 2>/dev/null || print_warning "No cameras detected yet (may need reboot)"
    elif command -v libcamera-hello &> /dev/null; then
        libcamera-hello --list-cameras 2>/dev/null || print_warning "No cameras detected yet (may need reboot)"
    else
        print_warning "Camera tools not installed - camera detection skipped"
    fi
    
    print_success "Camera dependencies installed"
}

# Create installation directory and clone/copy files
setup_installation() {
    print_step "Setting up installation directory..."
    
    # Create directory
    mkdir -p "$INSTALL_DIR"
    
    # Get script directory
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    
    # If running from git repo or local directory, copy files
    if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
        print_info "Installing from local files..."
        
        # Copy all project files
        cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR/" 2>/dev/null || true
        
        # Make sure config directory exists and has config
        mkdir -p "$INSTALL_DIR/config"
        if [[ -f "$SCRIPT_DIR/config/config.yaml" ]]; then
            cp "$SCRIPT_DIR/config/config.yaml" "$INSTALL_DIR/config/"
        fi
        
        # Remove any Windows line endings
        if command -v dos2unix &> /dev/null; then
            find "$INSTALL_DIR" -name "*.py" -exec dos2unix {} \; 2>/dev/null
            find "$INSTALL_DIR" -name "*.yaml" -exec dos2unix {} \; 2>/dev/null
            find "$INSTALL_DIR" -name "*.sh" -exec dos2unix {} \; 2>/dev/null
        fi
    else
        # Clone from repository
        print_info "Cloning from repository..."
        if [[ -d "$INSTALL_DIR/.git" ]]; then
            cd "$INSTALL_DIR"
            git pull
        else
            rm -rf "$INSTALL_DIR"
            git clone "$REPO_URL" "$INSTALL_DIR"
        fi
    fi
    
    # Ensure main.py is executable
    chmod +x "$INSTALL_DIR/src/main.py" 2>/dev/null || true
    
    # Set initial ownership to pirdfy user
    chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR" 2>/dev/null || true
    
    print_success "Installation directory set up"
}

# Create Python virtual environment
create_venv() {
    print_step "Creating Python virtual environment..."
    
    cd "$INSTALL_DIR"
    
    # Remove existing venv if present
    if [[ -d "venv" ]]; then
        print_info "Removing existing virtual environment..."
        rm -rf venv
    fi
    
    # Create venv with system site packages (required for picamera2)
    # picamera2 is installed via apt and needs system libraries
    print_info "Creating virtual environment with system packages..."
    python3 -m venv --system-site-packages venv
    
    # Activate virtual environment
    source venv/bin/activate
    
    # Upgrade pip and core tools
    print_info "Upgrading pip and setuptools..."
    pip install --upgrade pip wheel setuptools
    
    # Install requirements (picamera2 comes from system)
    print_info "Installing Python packages (this may take several minutes on Raspberry Pi)..."
    pip install --no-cache-dir -r requirements.txt
    
    # Verify picamera2 is available, try to install via pip if not
    print_info "Verifying picamera2 installation..."
    if python3 -c "import picamera2; print(f'picamera2 version: {picamera2.__version__}')" 2>/dev/null; then
        print_success "picamera2 is available"
    else
        print_warning "picamera2 not found via system packages, trying pip install..."
        pip install picamera2 2>/dev/null || print_warning "Could not install picamera2 via pip"
        
        # Check again
        if python3 -c "import picamera2" 2>/dev/null; then
            print_success "picamera2 installed via pip"
        else
            print_warning "picamera2 not available - camera features may not work"
            print_info "On Raspberry Pi OS: sudo apt install python3-picamera2"
            print_info "The system will use mock camera for testing"
        fi
    fi
    
    # Verify other key packages
    python3 -c "import cv2; print(f'OpenCV version: {cv2.__version__}')" 2>/dev/null || print_warning "OpenCV not fully installed"
    python3 -c "import flask; print(f'Flask version: {flask.__version__}')" 2>/dev/null || print_error "Flask not installed"
    
    # Set ownership of venv to pirdfy user
    chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/venv"
    
    print_success "Python environment created (owned by $SERVICE_USER)"
}

# Download YOLO model
download_model() {
    print_step "Downloading bird detection model..."
    
    cd "$INSTALL_DIR"
    source venv/bin/activate
    
    # Download YOLOv8n model (as pirdfy user so it's in the right location)
    # The model will be downloaded to the current directory
    print_info "Downloading YOLOv8n model (this may take a moment)..."
    sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/python" -c "
import os
os.chdir('$INSTALL_DIR')
from ultralytics import YOLO
model = YOLO('yolov8n.pt')
print('Model downloaded successfully')
" || {
        # Fallback: download as root and fix permissions
        print_warning "Downloading as root (fallback)..."
        python3 -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"
        # Move model to models dir and fix ownership
        mv -f yolov8n.pt "$INSTALL_DIR/models/" 2>/dev/null || true
        chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/models"
    }
    
    print_success "Detection model downloaded"
}

# Create data directories with proper permissions
create_directories() {
    print_step "Creating data directories..."
    
    # Create all required directories
    mkdir -p "$INSTALL_DIR/data/photos"
    mkdir -p "$INSTALL_DIR/data/birds"
    mkdir -p "$INSTALL_DIR/data/videos"
    mkdir -p "$INSTALL_DIR/data/annotated"
    mkdir -p "$INSTALL_DIR/logs"
    mkdir -p "$INSTALL_DIR/models"
    mkdir -p "$INSTALL_DIR/config"
    
    # Set ownership to pirdfy user
    chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"
    
    # Base directory permissions - readable by all, writable by owner
    chmod 755 "$INSTALL_DIR"
    chmod -R 755 "$INSTALL_DIR/src"
    chmod -R 755 "$INSTALL_DIR/config"
    
    # Data directories - writable by pirdfy user
    chmod 755 "$INSTALL_DIR/data"
    chmod 755 "$INSTALL_DIR/data/photos"
    chmod 755 "$INSTALL_DIR/data/birds"
    chmod 755 "$INSTALL_DIR/data/videos"
    chmod 755 "$INSTALL_DIR/data/annotated"
    
    # Logs directory - writable by pirdfy user
    chmod 755 "$INSTALL_DIR/logs"
    
    # Models directory
    chmod 755 "$INSTALL_DIR/models"
    
    # Create empty log files with correct permissions
    touch "$INSTALL_DIR/logs/pirdfy.log"
    chmod 644 "$INSTALL_DIR/logs/pirdfy.log"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/logs/pirdfy.log"
    
    touch "$INSTALL_DIR/logs/service.log"
    chmod 644 "$INSTALL_DIR/logs/service.log"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/logs/service.log"
    
    print_success "Directories created with correct permissions for user $SERVICE_USER"
}

# Create systemd service
create_service() {
    print_step "Creating systemd service..."
    
    # Build list of supplementary groups that exist
    SUPP_GROUPS="video"
    for grp in gpio i2c spi render; do
        if getent group "$grp" > /dev/null 2>&1; then
            SUPP_GROUPS="$SUPP_GROUPS $grp"
        fi
    done
    
    cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=Pirdfy Bird Feeder Camera Detector
After=network.target multi-user.target
Wants=network-online.target

[Service]
Type=simple
# Run as dedicated pirdfy user (not root)
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${INSTALL_DIR}

# Environment
Environment=PATH=${INSTALL_DIR}/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PYTHONUNBUFFERED=1
Environment=HOME=${INSTALL_DIR}

# Start command
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/src/main.py --config ${INSTALL_DIR}/config/config.yaml

# Restart policy
Restart=always
RestartSec=10
TimeoutStartSec=60
TimeoutStopSec=30

# Allow camera and hardware access via supplementary groups
SupplementaryGroups=${SUPP_GROUPS}

# Device access for Raspberry Pi camera
DeviceAllow=/dev/video* rw
DeviceAllow=/dev/vchiq rw
DeviceAllow=/dev/dma_heap/* rw
DeviceAllow=/dev/media* rw

# Security hardening (relaxed for camera access)
PrivateTmp=true
ProtectSystem=false
ProtectHome=read-only
NoNewPrivileges=false
ReadWritePaths=${INSTALL_DIR}/data ${INSTALL_DIR}/logs ${INSTALL_DIR}/models

# Logging - redirect to files
StandardOutput=append:${INSTALL_DIR}/logs/service.log
StandardError=append:${INSTALL_DIR}/logs/service.log

# Resource limits
LimitNOFILE=65536
MemoryMax=2G

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd
    systemctl daemon-reload
    
    # Enable service to start on boot
    systemctl enable ${SERVICE_NAME}
    
    print_success "Systemd service created (runs as user: ${SERVICE_USER})"
}

# Create convenience scripts
create_scripts() {
    print_step "Creating convenience scripts..."
    
    # Start script
    cat > /usr/local/bin/pirdfy-start << 'EOF'
#!/bin/bash
sudo systemctl start pirdfy
echo "Pirdfy started. Dashboard available at http://$(hostname -I | awk '{print $1}'):8080"
EOF
    chmod +x /usr/local/bin/pirdfy-start
    
    # Stop script
    cat > /usr/local/bin/pirdfy-stop << 'EOF'
#!/bin/bash
sudo systemctl stop pirdfy
echo "Pirdfy stopped."
EOF
    chmod +x /usr/local/bin/pirdfy-stop
    
    # Status script
    cat > /usr/local/bin/pirdfy-status << 'EOF'
#!/bin/bash
sudo systemctl status pirdfy
EOF
    chmod +x /usr/local/bin/pirdfy-status
    
    # Logs script
    cat > /usr/local/bin/pirdfy-logs << 'EOF'
#!/bin/bash
sudo journalctl -u pirdfy -f
EOF
    chmod +x /usr/local/bin/pirdfy-logs
    
    # Update script
    cat > /usr/local/bin/pirdfy-update << 'EOF'
#!/bin/bash
cd /opt/pirdfy
sudo systemctl stop pirdfy
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl start pirdfy
echo "Pirdfy updated and restarted."
EOF
    chmod +x /usr/local/bin/pirdfy-update
    
    # Fix permissions script
    cat > /usr/local/bin/pirdfy-fix-permissions << 'EOF'
#!/bin/bash
# Fix permissions for Pirdfy data and log directories
INSTALL_DIR="/opt/pirdfy"
SERVICE_USER="pirdfy"
SERVICE_GROUP="pirdfy"

echo "Fixing permissions for Pirdfy..."

# Ensure user exists
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Error: User $SERVICE_USER does not exist"
    exit 1
fi

# Fix ownership
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/data"
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/logs"
sudo chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/models"

# Fix directory permissions
sudo chmod 755 "$INSTALL_DIR"
sudo chmod 755 "$INSTALL_DIR/data"
sudo chmod -R 755 "$INSTALL_DIR/data/photos"
sudo chmod -R 755 "$INSTALL_DIR/data/birds"
sudo chmod -R 755 "$INSTALL_DIR/data/videos"
sudo chmod -R 755 "$INSTALL_DIR/data/annotated"
sudo chmod 755 "$INSTALL_DIR/logs"
sudo chmod 644 "$INSTALL_DIR/logs/"*.log 2>/dev/null

# Ensure pirdfy user is in video group
sudo usermod -aG video "$SERVICE_USER"

echo "✓ Permissions fixed for user $SERVICE_USER"
EOF
    chmod +x /usr/local/bin/pirdfy-fix-permissions
    
    # Test camera script
    cat > /usr/local/bin/pirdfy-test-camera << 'EOF'
#!/bin/bash
echo "Testing Raspberry Pi camera..."
if command -v rpicam-hello &> /dev/null; then
    rpicam-hello --list-cameras
    echo ""
    echo "Taking test photo..."
    rpicam-jpeg -o /tmp/pirdfy-test.jpg -t 1000
    if [[ -f /tmp/pirdfy-test.jpg ]]; then
        echo "✓ Test photo saved to /tmp/pirdfy-test.jpg"
        rm /tmp/pirdfy-test.jpg
    fi
elif command -v libcamera-hello &> /dev/null; then
    libcamera-hello --list-cameras
    echo ""
    echo "Taking test photo..."
    libcamera-jpeg -o /tmp/pirdfy-test.jpg -t 1000
    if [[ -f /tmp/pirdfy-test.jpg ]]; then
        echo "✓ Test photo saved to /tmp/pirdfy-test.jpg"
        rm /tmp/pirdfy-test.jpg
    fi
else
    echo "Camera tools not found. Install with: sudo apt install rpicam-apps"
fi
EOF
    chmod +x /usr/local/bin/pirdfy-test-camera
    
    print_success "Convenience scripts created"
}

# Configure hostname (optional)
configure_hostname() {
    print_step "Configuring hostname..."
    
    read -p "Set hostname to 'pirdfy'? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        hostnamectl set-hostname pirdfy
        
        # Update /etc/hosts
        sed -i 's/127.0.1.1.*/127.0.1.1\tpirdfy/' /etc/hosts
        
        print_success "Hostname set to 'pirdfy'"
    else
        print_info "Hostname unchanged"
    fi
}

# Verify installation
verify_installation() {
    print_step "Verifying installation..."
    
    local errors=0
    
    # Check service user exists
    if id "$SERVICE_USER" &>/dev/null; then
        print_success "Service user '$SERVICE_USER' exists"
    else
        print_error "Service user '$SERVICE_USER' not found"
        ((errors++))
    fi
    
    # Check service user is in video group
    if id "$SERVICE_USER" 2>/dev/null | grep -q video; then
        print_success "Service user is in video group"
    else
        print_error "Service user not in video group"
        ((errors++))
    fi
    
    # Check directories exist and are owned by service user
    for dir in data/photos data/birds data/videos data/annotated logs; do
        if [[ -d "$INSTALL_DIR/$dir" ]]; then
            owner=$(stat -c '%U' "$INSTALL_DIR/$dir")
            if [[ "$owner" == "$SERVICE_USER" ]]; then
                print_success "Directory $dir owned by $SERVICE_USER"
            else
                print_warning "Directory $dir owned by $owner (expected $SERVICE_USER)"
            fi
        else
            print_error "Directory $dir does not exist"
            ((errors++))
        fi
    done
    
    # Check log file is writable by service user
    if sudo -u "$SERVICE_USER" touch "$INSTALL_DIR/logs/test.log" 2>/dev/null; then
        rm -f "$INSTALL_DIR/logs/test.log"
        print_success "Log directory is writable by $SERVICE_USER"
    else
        print_error "Cannot write to log directory as $SERVICE_USER"
        ((errors++))
    fi
    
    # Check config file exists
    if [[ -f "$INSTALL_DIR/config/config.yaml" ]]; then
        print_success "Configuration file exists"
    else
        print_error "Configuration file missing"
        ((errors++))
    fi
    
    # Check virtual environment
    if [[ -f "$INSTALL_DIR/venv/bin/python" ]]; then
        print_success "Virtual environment exists"
    else
        print_error "Virtual environment not found"
        ((errors++))
    fi
    
    # Check systemd service
    if systemctl is-enabled ${SERVICE_NAME} &>/dev/null; then
        print_success "Systemd service is enabled"
    else
        print_error "Systemd service not enabled"
        ((errors++))
    fi
    
    # Check camera access
    if [[ -e /dev/video0 ]] || [[ -e /dev/vchiq ]]; then
        print_success "Camera device detected"
    else
        print_warning "No camera device found (may need reboot or camera not connected)"
    fi
    
    if [[ $errors -gt 0 ]]; then
        print_warning "Installation completed with $errors issue(s)"
    else
        print_success "All verification checks passed"
    fi
}

# Print final instructions
print_instructions() {
    IP_ADDR=$(hostname -I | awk '{print $1}')
    
    echo -e "\n${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}  ✓ Pirdfy Installation Complete!${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
    
    echo -e "${BLUE}📍 Installation Directory:${NC} $INSTALL_DIR"
    echo -e "${BLUE}📊 Dashboard URL:${NC} http://${IP_ADDR}:8080"
    echo -e "${BLUE}📄 Configuration:${NC} $INSTALL_DIR/config/config.yaml\n"
    
    echo -e "${YELLOW}Quick Commands:${NC}"
    echo -e "  pirdfy-start           - Start the service"
    echo -e "  pirdfy-stop            - Stop the service"
    echo -e "  pirdfy-status          - Check service status"
    echo -e "  pirdfy-logs            - View live logs"
    echo -e "  pirdfy-update          - Update to latest version"
    echo -e "  pirdfy-test-camera     - Test camera connectivity"
    echo -e "  pirdfy-fix-permissions - Fix file permissions\n"
    
    echo -e "${YELLOW}Manual Control:${NC}"
    echo -e "  sudo systemctl start pirdfy"
    echo -e "  sudo systemctl stop pirdfy"
    echo -e "  sudo systemctl restart pirdfy\n"
    
    read -p "Start Pirdfy now? (Y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Nn]$ ]]; then
        systemctl start ${SERVICE_NAME}
        print_success "Pirdfy is now running!"
        echo -e "\n${GREEN}Open your browser and go to: http://${IP_ADDR}:8080${NC}\n"
    else
        print_info "Run 'pirdfy-start' when ready to start the service"
    fi
}

# Main installation flow
main() {
    print_header
    
    check_root
    check_requirements
    create_service_user
    install_dependencies
    install_picamera2_deps
    setup_installation
    create_directories
    create_venv
    download_model
    create_service
    create_scripts
    verify_installation
    configure_hostname
    print_instructions
}

# Run main
main "$@"
