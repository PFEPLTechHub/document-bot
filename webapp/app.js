document.addEventListener('DOMContentLoaded', function () {
    // TEST FETCH: Check if /api/history is reachable
    fetch('/api/history')
      .then(r => r.json())
      .then(data => console.log('TEST FETCH /api/history:', data))
      .catch(e => {
        console.error('TEST FETCH ERROR:', e);
        const historyTable = document.getElementById('historyTable');
        if (historyTable) {
          historyTable.innerHTML = `<tr><td colspan='6' class='text-danger text-center'>Cannot reach /api/history: ${e}</td></tr>`;
        }
      });

    // Initialize Telegram WebApp
    const tg = window.Telegram.WebApp;
    tg.expand();

    // DOM Elements
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    const uploadBtn = document.getElementById('upload-btn');
    const uploadProgress = document.getElementById('upload-progress');
    const uploadHistory = document.getElementById('upload-history');
    const userInfo = document.getElementById('user-info');
    const historyTable = document.getElementById('historyTable');
    const monthFilter = document.getElementById('monthFilter');
    const employeeFilter = document.getElementById('employeeFilter');
    const statusFilter = document.getElementById('statusFilter');
    const pagination = document.getElementById('pagination');

    // State
    let currentPage = 1;
    const itemsPerPage = 10;
    let allHistory = [];
    let filteredHistory = [];

    // Set default to current month
    if (monthFilter) {
        const now = new Date();
        const monthString = now.toISOString().slice(0, 7); // 'YYYY-MM'
        monthFilter.value = monthString;
    }

    // Initialize user info
    function initUserInfo() {
        if (!userInfo) return;
        const user = tg.initDataUnsafe?.user;
        if (user) {
            userInfo.innerHTML = `
                <p>Welcome, ${user.first_name} ${user.last_name || ''}</p>
                <p>Username: @${user.username || 'N/A'}</p>
            `;
        }
    }

    // Handle file upload
    async function handleFileUpload(files) {
        // Check if adding these files would exceed the limit
        const currentFiles = document.querySelectorAll('.file-item').length;
        if (currentFiles + files.length > 4) {
            alert(`Cannot upload more files. You can only upload up to 4 files (${currentFiles}/4 used).`);
            return;
        }

        // If we're at 3 files and trying to upload multiple files, prevent it
        if (currentFiles === 3 && files.length > 1) {
            alert(`You can only upload 1 more file (${currentFiles}/4 used).`);
            return;
        }

        for (const file of files) {
            const formData = new FormData();
            formData.append('file', file);

            // Create file item without progress bar
            const fileId = `file-${Date.now()}`;
            const fileHtml = `
                <div class="file-item">
                    <div class="file-info">
                        <div class="file-name">${file.name}</div>
                        <div class="file-size">${formatFileSize(file.size)}</div>
                    </div>
                    <div id="${fileId}" class="file-status">Uploading...</div>
                </div>
            `;
            uploadProgress.insertAdjacentHTML('beforeend', fileHtml);

            try {
                const response = await fetch('/api/upload', {
                    method: 'POST',
                    body: formData
                });

                if (response.ok) {
                    const result = await response.json();
                    document.getElementById(fileId).textContent = 'Uploaded successfully';
                    document.getElementById(fileId).className = 'file-status status-success';
                    loadHistory();
                } else {
                    throw new Error('Upload failed');
                }
            } catch (error) {
                document.getElementById(fileId).textContent = 'Upload failed';
                document.getElementById(fileId).className = 'file-status status-error';
                console.error('Upload error:', error);
            }
        }
    }

    // Format date
    function formatDate(dateString) {
        // Parse the date string - server now sends IST time
        const date = new Date(dateString);
        
        // Get the local date components
        const day = date.getDate();
        const hours = date.getHours();
        const minutes = date.getMinutes();
        const ampm = hours >= 12 ? 'PM' : 'AM';
        const formattedHours = hours % 12 || 12;
        const formattedMinutes = minutes.toString().padStart(2, '0');
        
        return `${day}, ${formattedHours}:${formattedMinutes} ${ampm}`;
    }

    // Format file size
    function formatFileSize(bytes) {
        if (bytes === 0) return '0 Bytes';
        const k = 1024;
        const sizes = ['Bytes', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    // Load history
    async function loadHistory() {
        console.log('Calling /api/history from loadHistory()...');
        try {
            const response = await fetch('/api/history');
            if (response.ok) {
                allHistory = await response.json();
                console.log('Received history data:', allHistory); // Debug log
                if (!Array.isArray(allHistory)) {
                    showError('Invalid data format received from server.');
                    console.error('Invalid data:', allHistory);
                    allHistory = [];
                }
                populateFilters();
                applyFilters();
            } else {
                showError('Failed to load history: ' + response.status);
                console.error('Failed to load history:', response.status);
            }
        } catch (error) {
            showError('Error loading history: ' + error);
            console.error('Error loading history:', error);
        }
    }

    // Show error message in table
    function showError(msg) {
        if (historyTable) {
            historyTable.innerHTML = `<tr><td colspan="6" class="text-danger text-center">${msg}</td></tr>`;
        }
    }

    // Populate filters
    function populateFilters() {
        if (!employeeFilter) {
            console.log('Employee filter element not found'); // Debug log
            return;
        }
        console.log('Populating filters with history:', allHistory); // Debug log
        // Populate employees
        const employees = new Set();
        allHistory.forEach(item => {
            if (item.employee_name) {
                employees.add(item.employee_name);
            }
        });
        console.log('Found employees:', Array.from(employees)); // Debug log
        const employeeOptions = Array.from(employees)
            .sort()
            .map(emp => `<option value="${emp}">${emp}</option>`)
            .join('');
        employeeFilter.innerHTML = '<option value="">All Employees</option>' + employeeOptions;
        if (employees.size === 0) {
            employeeFilter.innerHTML += '<option disabled>No employees found</option>';
        }
    }

    // Apply filters
    function applyFilters() {
        console.log('Applying filters with allHistory:', allHistory); // Debug log
        const selectedMonth = monthFilter ? monthFilter.value : '';
        const selectedEmployee = employeeFilter ? employeeFilter.value : '';
        const selectedStatus = statusFilter ? statusFilter.value : '';

        filteredHistory = allHistory.filter(item => {
            const itemDate = new Date(item.session_date);
            const itemMonth = itemDate.toISOString().slice(0, 7);
            const monthMatch = !selectedMonth || itemMonth === selectedMonth;
            const employeeMatch = !selectedEmployee || item.employee_name === selectedEmployee;
            const statusMatch = !selectedStatus || item.validation_status === selectedStatus;
            return monthMatch && employeeMatch && statusMatch;
        });
        console.log('Filtered history:', filteredHistory); // Debug log

        currentPage = 1;
        displayHistory();
        updatePagination();
    }

    // Display history
    function displayHistory() {
        console.log('Displaying history with filteredHistory:', filteredHistory); // Debug log
        if (!historyTable) {
            console.log('History table element not found!'); // Debug log
            return;
        }
        const start = (currentPage - 1) * itemsPerPage;
        const end = start + itemsPerPage;
        const pageItems = filteredHistory.slice(start, end);
        console.log('Page items to display:', pageItems); // Debug log

        if (pageItems.length === 0) {
            historyTable.innerHTML = `
                <tr>
                    <td colspan="6" class="text-center">No records found</td>
                </tr>
            `;
            return;
        }

        historyTable.innerHTML = pageItems.map(item => `
            <tr>
                <td>${formatDate(item.session_date)}</td>
                <td>${item.employee_name || 'Unknown'}</td>
                <td>${item.original_name}</td>
                <td>${formatFileSize(item.file_size)}</td>
                <td>
                    <span class="status-badge status-${item.validation_status}">
                        ${item.validation_status}
                    </span>
                </td>
                <td>
                    <button class="action-btn" onclick="viewDetails('${item.id}')">
                        <i class="bi bi-eye"></i> View
                    </button>
                </td>
            </tr>
        `).join('');
    }

    // Update pagination
    function updatePagination() {
        if (!pagination) return;
        const totalPages = Math.ceil(filteredHistory.length / itemsPerPage);
        if (totalPages <= 1) {
            pagination.innerHTML = '';
            return;
        }
        let paginationHtml = '';
        // Previous button
        paginationHtml += `
            <li class="page-item ${currentPage === 1 ? 'disabled' : ''}">
                <a class="page-link" href="#" onclick="changePage(${currentPage - 1})">Previous</a>
            </li>
        `;
        // Page numbers
        for (let i = 1; i <= totalPages; i++) {
            paginationHtml += `
                <li class="page-item ${currentPage === i ? 'active' : ''}">
                    <a class="page-link" href="#" onclick="changePage(${i})">${i}</a>
                </li>
            `;
        }
        // Next button
        paginationHtml += `
            <li class="page-item ${currentPage === totalPages ? 'disabled' : ''}">
                <a class="page-link" href="#" onclick="changePage(${currentPage + 1})">Next</a>
            </li>
        `;
        pagination.innerHTML = paginationHtml;
    }

    // Change page
    window.changePage = function(page) {
        if (page < 1 || page > Math.ceil(filteredHistory.length / itemsPerPage)) return;
        currentPage = page;
        displayHistory();
        updatePagination();
    };

    // View details
    window.viewDetails = function(id) {
        const item = allHistory.find(h => h.id === id);
        if (item) {
            tg.showPopup({
                title: 'File Details',
                message: `
                    File: ${item.original_name}
                    Size: ${formatFileSize(item.file_size)}
                    Status: ${item.validation_status}
                    Date: ${formatDate(item.session_date)}
                    ${item.validation_errors ? `\nErrors: ${item.validation_errors}` : ''}
                `,
                buttons: [{ type: 'close' }]
            });
        }
    };

    // Event listeners
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--tg-theme-button-color)';
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.style.borderColor = 'var(--tg-theme-hint-color)';
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = 'var(--tg-theme-hint-color)';
        handleFileUpload(e.dataTransfer.files);
    });

    uploadBtn.addEventListener('click', () => {
        fileInput.click();
    });

    fileInput.addEventListener('change', (e) => {
        handleFileUpload(e.target.files);
    });

    if (monthFilter) monthFilter.addEventListener('change', applyFilters);
    if (employeeFilter) employeeFilter.addEventListener('change', applyFilters);
    if (statusFilter) statusFilter.addEventListener('change', applyFilters);

    // Initialize
    initUserInfo();
    loadHistory();
}); 