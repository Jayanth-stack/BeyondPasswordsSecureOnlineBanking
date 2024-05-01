//prevent browser back
history.pushState(null, null, document.URL);
window.addEventListener('popstate', function () {
    history.pushState(null, null, document.URL);
});

//prevent right-click
document.addEventListener("contextmenu", function(e){
  e.preventDefault();
})

const homeURL = 'http://127.0.0.1:5000/';

var userid, usertype, firstname, midname, lastname, email, contact, dob, ssn, address;

function getUser() {
  console.log("getadmin called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const loadUserData = {
    employee_id : '4',
    usertype : 'admin'
  };

  fetch(homeURL+'loadEmployee', {
    method : 'post',
    body : JSON.stringify(loadUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("get admin response received");
    if (response.redirected) {
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    console.log(data);
    appendPrimaryData(data);
  }).catch(function(error){
    console.error(error);
  });
}

function appendPrimaryData(data) {
  first_name = data.Info.first_name;
  midname = data.Info.middle_name;
  lastname = data.Info.last_name;
  email = data.Info.email_id;
  contact = data.Info.contact_no;
  dob = data.Info.dob;
  ssn = data.Info.ssn;
  address = data.Info.address;

  document.getElementById("account_first_name").value = first_name;
  document.getElementById("account_middle_name").value = midname;
  document.getElementById("account_last_name").value = lastname;
  document.getElementById("account_email_id").value = email;
  document.getElementById("account_contact_no").value = contact;
  document.getElementById("account_dob").value = dob;
  document.getElementById("account_ssn").value = ssn;
  document.getElementById("account_address").value = address;

  fillPendingReqTbl(data);
}

function fillPendingReqTbl(data){
  var table = document.getElementById('emp_reqs_tbl');
  var rowCount = table.rows.length;

  var selection = document.getElementById('employee_req_id');
  try {
    for(var i=1; i<rowCount; i++) {
      table.deleteRow(i);
      selection.remove(i);
      rowCount--;
      i--;
    }
  }catch(e) {
    alert(e);
  }

  if(data.UpdateInfo != 'None') {
    for (var i=0; i< data.UpdateInfo.length; i++){
      var rowCount = table.rows.length;
      var row = table.insertRow(rowCount);
      var cell1 = row.insertCell(0);
      cell1.innerHTML = data.UpdateInfo[i][0];

      var cell2 = row.insertCell(1);
      cell2.innerHTML = data.UpdateInfo[i][2];

      var cell3 = row.insertCell(2);
      cell3.innerHTML = 'Contact No : ' + data.UpdateInfo[i][3] + '<br>' +
      'Email-id : ' + data.UpdateInfo[i][4] + '<br>' +
      'Address : ' + data.UpdateInfo[i][5];

      var option = document.createElement("OPTION");
      option.innerHTML = data.UpdateInfo[i][0];
      option.value = data.UpdateInfo[i][0];
      //Add the Option element to DropDownList.
      selection.options.add(option);
    }
  }
}

function logout(){
  console.log("logout called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const logoutUserData = {
    userid : userid
  };

  fetch(homeURL+'logout', {
    method : 'post',
    body : JSON.stringify(logoutUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("logout response received");
    if (response.redirected) {
      window.location.href = response.url;
    }
    else {
      window.alert("Oops we encountered an error!");
    }
  }).catch(function(error){
    console.error(error);
  });
}

function getEmployee(employee_id) {
  console.log("getEmployee called");

  const loadEmployeeData = {
    userid : userid,
    emp_id : employee_id
  };

  fetch(homeURL+'getEmployee', {
    method : 'post',
    body : JSON.stringify(loadEmployeeData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getEmployee response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    appendSecondaryData(employee_id, data);
  }).catch(function(error){
    console.error(error);
  });
}

function getModifyEmployee(employee_id) {
  console.log("getemployee called");

  const loadEmployeeData = {
    userid : userid,
    emp_id : employee_id
  };

  fetch(homeURL+'getEmployee', {
    method : 'post',
    body : JSON.stringify(loadEmployeeData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getemployee response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    document.getElementById("modify_first_name").value = data.Info.first_name;
    document.getElementById("modify_middle_name").value = data.Info.middle_name;
    document.getElementById("modify_last_name").value = data.Info.last_name;
    document.getElementById("modify_email_id").value = data.Info.email_id;
    document.getElementById("modify_contact_no").value = data.Info.contact_no;
    document.getElementById("modify_dob").value = data.Info.dob;
    document.getElementById("modify_ssn").value = data.Info.ssn;
    document.getElementById("modify_address").value = data.Info.address;
    document.getElementById("modify_tier").value = data.Info.tier;
  }).catch(function(error){
    console.error(error);
  });
}

function appendSecondaryData(employee_id, data) {

  document.getElementById("employee_id").innerHTML = employee_id;
  document.getElementById("first_name").innerHTML = data.Info.first_name;
  document.getElementById("middle_name").innerHTML = data.Info.middle_name;
  document.getElementById("last_name").innerHTML = data.Info.last_name;
  document.getElementById("email_id").innerHTML = data.Info.email_id;
  document.getElementById("contact_no").innerHTML = data.Info.contact_no;
  document.getElementById("dob").innerHTML = data.Info.dob;
  document.getElementById("ssn").innerHTML = data.Info.ssn;
  document.getElementById("address").innerHTML = data.Info.address;
  document.getElementById("tier").innerHTML = data.Info.tier;

  if($('#cust_details_card').css('display')=='none'){
    $('#cust_details_card').show();
  }
}

function approveEmployeeRequest(userid, request_id) {
  console.log("approveEmployeeReq called");

  const approveEmployeeReqData = {
    userid : userid,
    update_req_no : request_id
  };

  fetch(homeURL+'approveUpdateInfo', {
    method : 'post',
    body : JSON.stringify(approveEmployeeReqData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("approveEmployeeReq response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function denyEmployeeRequest(userid, request_id) {
  console.log("denyEmployeeReq called");

  const denyEmployeeReqData = {
    userid : userid,
    update_req_no : request_id
  };

  fetch(homeURL+'denyUpdateInfo', {
    method : 'post',
    body : JSON.stringify(denyEmployeeReqData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("denyEmployeeReq response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function register_employee() {
  console.log("register_employee called");

  const register_employeeData = {
    userid : $('#create_userid').val(),
    password : $('#create_password').val(),
    email : $('#create_email_id').val(),
    firstname : $('#create_first_name').val(),
    midname : $('#create_middle_name').val(),
    lastname : $('#create_last_name').val(),
    phone : $('#create_contact_no').val(),
    dob : $('#create_dob').val(),
    ssn : $('#create_ssn').val(),
    address : $('#create_address').val(),
    tier : $('#create_tier').val()
  };

  fetch(homeURL+'registerEmployee', {
    method : 'post',
    body : JSON.stringify(register_employeeData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("register_employee response received");
    return response.json();
  }).then(function (data) {
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function modify_employee() {
  console.log("modify_employee called");

  const modify_employeeData = {
    userid : userid,
    emp_id : $('#modify_userid').val(),
    last_name : $('#modify_last_name').val(),
    middle_name : $('#modify_middle_name').val(),
    first_name : $('#modify_first_name').val(),
    contact_no : $('#modify_contact_no').val(),
    email_id : $('#modify_email_id').val(),
    ssn : $('#modify_ssn').val(),
    dob : $('#modify_dob').val(),
    address : $('#modify_address').val(),
    tier : $('#modify_tier').val()
  };

  fetch(homeURL+'modifyEmployee', {
    method : 'post',
    body : JSON.stringify(modify_employeeData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("modify_employee response received");
    if (response.redirected) {
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function updateAdmin() {
  console.log("modify_employee called");

  const modify_employeeData = {
    userid : userid,
    emp_id : userid,
    last_name : $('#account_last_name').val(),
    middle_name : $('#account_middle_name').val(),
    first_name : $('#account_first_name').val(),
    contact_no : $('#account_contact_no').val(),
    email_id : $('#account_email_id').val(),
    ssn : $('#account_ssn').val(),
    dob : $('#account_dob').val(),
    address : $('#account_address').val(),
    tier : 3
  };

  fetch(homeURL+'modifyEmployee', {
    method : 'post',
    body : JSON.stringify(modify_employeeData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("modify_employee response received");
    if (response.redirected) {
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function deleteUser(userid, delete_id) {
    console.log("deleteUser called");

    // Disable the delete button to prevent multiple clicks
    const deleteButton = document.getElementById('delete_employee_id_btn');
    deleteButton.disabled = true;

    const deleteUserData = {
        userid: userid,
        employee_id: delete_id
    };
    console.log("deleteUserData:", deleteUserData);

    fetch(homeURL + 'deactivateEmployee', {
        method: 'POST',
        body: JSON.stringify(deleteUserData),
        headers: {
            'Content-Type': 'application/json',
        }
    }).then(function(response) {
        console.log("deleteUser response received");
        if (!response.ok) {
            throw new Error('Server responded with status ' + response.status);
        }
        return response.json();
    }).then(function(data) {
        console.log(data);
        window.alert(data.message);
    }).catch(function(error) {
        console.error('Error occurred:', error);
        window.alert('Error: ' + error.message);
    }).finally(() => {
        // Re-enable the delete button
        deleteButton.disabled = false;
    });
}


function changePassword(userid, oldPassword, newPassword) {
  const resetpwData = {
    userid : userid,
    oldPassword : oldPassword,
    newPassword : newPassword,
    requester : 'Employee',
    flag : 1
  };

  fetch(homeURL+'resetPassword', {
    method : 'post',
    body : JSON.stringify(resetpwData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("resetpw response received");
    if(response.redirected){
      localStorage.setItem('loggedStatus', '0');
      window.location.href = response.url;
    }
    else {
      return response.json();
    }
  }).then(function (data) {
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function getSysLog() {
  console.log("syslog called");

  const syslogData = {
    userid : userid
  };

  fetch(homeURL+'getSystemLogs', {
    method : 'post',
    body : JSON.stringify(syslogData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("syslogData response received");
    return response.blob();
  }).then(function (blob) {
    console.log('trying');
    var logDownload = document.createElement("a");
    logDownload.href = URL.createObjectURL(blob);
    logDownload.setAttribute("download", "SystemLogs.txt");
    logDownload.click();
    // var file = window.URL.createObjectURL(blob);
    // window.location.assign(file);
  }).catch(function(error){
    console.error(error);
  });
}

$(document).ready(function() {
  userid = localStorage.getItem('user');
  usertype = localStorage.getItem('usertype');

    $('#logout_btn').on('click', function(){
      logout();
    });

    $('#account_details_btn').on('click', function(){
      getUser();
      if($('#account_details_pane').css('display')=='none') {
        $('#account_details_pane').show().siblings('div').hide();
      }
      $('#empl_accs_btn').css('background-color','maroon');
      $('#empl_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
      $('#sys_log_btn').css('background-color','maroon');
    });
    $('#empl_requests_btn').on('click', function(){
      if($('#empl_requests_pane').css('display')=='none') {
        $('#empl_requests_pane').show().siblings('div').hide();
      }
      $('#empl_accs_btn').css('background-color','maroon');
      $('#empl_requests_btn').css('background-color','#FF6600');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
      $('#sys_log_btn').css('background-color','maroon');
    });
    $('#app_dec_requests_btn').on('click', function(){
      if($('#app_dec_requests_pane').css('display')=='none') {
        $('#app_dec_requests_pane').show().siblings('div').hide();
      }
      $('#empl_accs_btn').css('background-color','maroon');
      $('#empl_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','#FF6600');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
      $('#sys_log_btn').css('background-color','maroon');
    });
    $('#empl_accs_btn').on('click', function(){
      getUser();
      if($('#empl_accs_pane').css('display')=='none') {
        $('#empl_accs_pane').show().siblings('div').hide();
      }
      $('#empl_accs_btn').css('background-color','#FF6600');
      $('#empl_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
      $('#sys_log_btn').css('background-color','maroon');
    });
    $('#create_accs_btn').on('click', function(){
      if($('#create_accs_pane').css('display')=='none') {
        $('#create_accs_pane').show().siblings('div').hide();
      }
      $('#empl_accs_btn').css('background-color','maroon');
      $('#empl_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','#FF6600');
      $('#modify_accs_btn').css('background-color','maroon');
      $('#sys_log_btn').css('background-color','maroon');
    });
    $('#modify_accs_btn').on('click', function(){
      if($('#modify_accs_pane').css('display')=='none') {
        $('#modify_accs_pane').show().siblings('div').hide();
      }
      $('#empl_accs_btn').css('background-color','maroon');
      $('#empl_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','#FF6600');
      $('#sys_log_btn').css('background-color','maroon');
    });
    $('#sys_log_btn').on('click', function(){
      if($('#sys_log_pane').css('display')=='none') {
        $('#sys_log_pane').show().siblings('div').hide();
      }
      $('#empl_accs_btn').css('background-color','maroon');
      $('#empl_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
      $('#sys_log_btn').css('background-color','#FF6600');
    });
    $('#empl_accs_btn').click();
    $(".loader-wrapper").delay( 1000 ).fadeOut("slow");
    $('#home_logo').on('click', function(){
      $('#empl_accs_btn').click();
    });
    $('#update_info_btn').on('click', function(){
      console.log("updateInfo function");
      updateAdmin();
    });
    $('#employee_id_input_btn').on('click', function(){
      if($('#employee_id_input').val() == ''){
        window.alert('No input!');
      }
      else {
        getEmployee($('#employee_id_input').val());
      }
    });
    $('#modify_employee_account_get_btn').on('click', function(){
      if($('#modify_userid').val() == ''){
        window.alert('No input!');
      }
      else {
        getModifyEmployee($('#modify_userid').val());
      }
    });
    $('#employee_id_clear_btn').on('click', function(){
      $('#cust_details_card').hide();
    });
    $('#approve_req_btn').on('click', function(){
      if($('#employee_req_id').val() == 'select'){
        window.alert('No input!');
      }
      else {
        approveEmployeeRequest(userid, $('#employee_req_id').val());
      }
    });
    $('#deny_req_btn').on('click', function(){
      if($('#employee_req_id').val() == 'select'){
        window.alert('No input!');
      }
      else {
        denyEmployeeRequest(userid, $('#employee_req_id').val());
      }
    });
    $('#create_employee_form').on('submit', function(e){
      e.preventDefault();
      register_employee();
    });
    $('#modify_employee_form').on('submit', function(e){
      e.preventDefault();
      modify_employee();
    });
    $('#delete_employee_id_btn').on('click', function(){
      if($('#delete_employee_id').val() == ''){
        window.alert('No input!');
      }
      else {
        deleteUser(userid, $('#delete_employee_id').val());
      }
    });
    $('#changePW_btn').on('click', function(){
      if($('#changePW_oldPW').val() == '' || $('#changePW_newPW').val() == '' || $('#changePW_confirmPW').val() == '') {
        window.alert("Empty Input!");
      }
      else {
        if($('#changePW_newPW').val() == $('#changePW_oldPW').val()){
          window.alert("Old & New Password shouldn't be the same!");
        }
        else {
          if($('#changePW_newPW').val() == $('#changePW_confirmPW').val()){
            changePassword(userid, $('#changePW_oldPW').val(), $('#changePW_newPW').val());
            $('#changePW_close').click();
          }
          else {
            window.alert("Re-entered password doesn't match new password!");
          }
        }
      }
    });
    $('#sys_logs_btn').on('click', function(){
      getSysLog();
    });
  });
