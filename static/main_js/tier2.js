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
  console.log("gettier2 called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const loadUserData = {
    employee_id : userid,
    usertype : 'tier2'
  };

  fetch(homeURL+'loadEmployee', {
    method : 'post',
    body : JSON.stringify(loadUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("gettier2 response received");
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

  fillPendingTransTbl(data);
}

function fillPendingTransTbl(data){
  var table = document.getElementById('pending_requests_tbl');
  var rowCount = table.rows.length;

  var selection = document.getElementById('customer_trans_no');
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

  if(data.FundsRequests != 'None') {
    for (var i=0; i< data.FundsRequests.length; i++){
      var rowCount = table.rows.length;
      var row = table.insertRow(rowCount);
      var cell1 = row.insertCell(0);
      cell1.innerHTML = data.FundsRequests[i][0];

      var cell2 = row.insertCell(1);
      cell2.innerHTML = data.FundsRequests[i][1];

      var cell3 = row.insertCell(2);
      cell3.innerHTML = data.FundsRequests[i][2];

      var cell4 = row.insertCell(3);
      cell4.innerHTML = data.FundsRequests[i][5];

      var option = document.createElement("OPTION");
      option.innerHTML = data.FundsRequests[i][0];
      option.value = data.FundsRequests[i][0];
      //Add the Option element to DropDownList.
      selection.options.add(option);
    }
  }
}

function logout() {
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

function updateInfo(userid, email_info, contact_info, address_info) {
  console.log("updateInfo called");

  const updateInfoData = {
    userid : userid,
    email : email_info,
    contact_no : contact_info,
    address : address_info,
    requester : 'Employee'
  };

  fetch(homeURL+'updateInfo', {
    method : 'post',
    body : JSON.stringify(updateInfoData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("updateInfo response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Update Info Request Placed'){
      window.alert('Update Info Request Placed!');
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function getCustomer(customer_id) {
  console.log("getcustomer called");

  const loadCustomerData = {
    userid: userid,
    customer_id : customer_id
  };

  fetch(homeURL+'getCustomer', {
    method : 'post',
    body : JSON.stringify(loadCustomerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getcustomer response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    appendSecondaryData(customer_id, data);
  }).catch(function(error){
    console.error(error);
  });
}

function getModifyCustomer(customer_id) {
  console.log("getcustomer called");

  const loadCustomerData = {
    userid: userid,
    customer_id : customer_id
  };

  fetch(homeURL+'getCustomer', {
    method : 'post',
    body : JSON.stringify(loadCustomerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("getcustomer response received");
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
  }).catch(function(error){
    console.error(error);
  });
}

function appendSecondaryData(customer_id, data) {

  document.getElementById("customer_id").innerHTML = customer_id;
  document.getElementById("first_name").innerHTML = data.Info.first_name;
  document.getElementById("middle_name").innerHTML = data.Info.middle_name;
  document.getElementById("last_name").innerHTML = data.Info.last_name;
  document.getElementById("email_id").innerHTML = data.Info.email_id;
  document.getElementById("contact_no").innerHTML = data.Info.contact_no;
  document.getElementById("dob").innerHTML = data.Info.dob;
  document.getElementById("ssn").innerHTML = data.Info.ssn;
  document.getElementById("address").innerHTML = data.Info.address;

  fillCustomerAccTbl(data);
  if($('#cust_details_card').css('display')=='none'){
    $('#cust_details_card').show();
  }
  if($('#cust_accounts_tbl').css('display')=='none'){
    $('#cust_details_tbl').show();
  }
}

function fillCustomerAccTbl(data){
  var table = document.getElementById('cust_accounts_tbl');
  var rowCount = table.rows.length;
  try {
    for(var i=1; i<rowCount; i++) {
      table.deleteRow(i);
      rowCount--;
      i--;
    }
  }catch(e) {
    alert(e);
  }

  if(data.Accounts.savings != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.savings.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.savings.Balance;
  }
  if(data.Accounts.checkin != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.checkin.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.checkin.Balance;
  }
  if(data.Accounts.credit != "None"){
    var rowCount = table.rows.length;
    var row = table.insertRow(rowCount);
    var cell1 = row.insertCell(0);
	  cell1.innerHTML = data.Accounts.credit.Account;

	  var cell2 = row.insertCell(1);
	  cell2.innerHTML = 'Savings';

	  var cell3 = row.insertCell(2);
	  cell3.innerHTML = data.Accounts.credit.Balance;
  }
}

function approve_request(userid, xactno) {
  console.log("approve request called");

  const approveRequestData = {
    userid : userid,
    transaction_no : xactno
  };

  fetch(homeURL+'approveRequestEmp', {
    method : 'post',
    body : JSON.stringify(approveRequestData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("approveRequest response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Invalid transaction_no'){
      window.alert('Invalid Transaction No input!');
    }
    else if(data.message == 'done'){
      window.alert('Approved!');
    }
    else{
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function deny_request(userid, xactno) {
  console.log("deny request called");

  const denyRequestData = {
    userid : userid,
    transaction_no : xactno
  };

  fetch(homeURL+'denyRequest', {
    method : 'post',
    body : JSON.stringify(denyRequestData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("denyRequest response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Invalid transaction_no'){
      window.alert('Invalid Transaction No input!');
    }
    else if(data.message == 'Request Cancelled'){
      window.alert('Denied!');
    }
    else{
      window.alert(data.message);
    }
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function register_customer() {
  console.log("register_customer called");
  a = $('#create_userid').val().toString();
  console.log("i'm, " , a);

  const register_customerData = {
    'userid' : $('#create_userid').val().toString(),
    'empid' : $('#create_userid').val().toString(),
    'password' : $('#create_password').val().toString(),
    'email' : $('#create_email_id').val().toString(),
    'firstname' : $('#create_first_name').val().toString(),
    'midname' : $('#create_middle_name').val().toString(),
    'lastname' : $('#create_last_name').val().toString(),
    'phone' : $('#create_contact_no').val().toString(),
    'dob' : $('#create_dob').val().toString(),
    'ssn' : $('#create_ssn').val().toString(),
    'address' : $('#create_address').val().toString()
  };

  fetch(homeURL+'registerCustomer', {
    method : 'post',
    body : JSON.stringify(register_customerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("register_customer response received");
    return response.json();
  }).then(function (data) {
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function newAcc(customer_id, account_type) {
  console.log("newAcc called");

  const newAccData = {
    userid : userid,
    customer_id : customer_id,
    account_type : account_type
  };

  fetch(homeURL+'openNewAccount', {
    method : 'post',
    body : JSON.stringify(newAccData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("newAcc response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message[0] == 'Customer already have '){
      window.alert('You already have a '+ data.message[1] + ' account!');
    }
    else if(data.message[0] == 'Done'){
      window.alert('Congratulations! You have a new account with us.');
    }
  }).catch(function(error){
    console.error(error);
  });
}

function modify_customer() {
  console.log("modify_customer called");

  const modify_customerData = {
    userid : userid,
    customer_id : $('#modify_userid').val(),
    last_name : $('#modify_last_name').val(),
    middle_name : $('#modify_middle_name').val(),
    first_name : $('#modify_first_name').val(),
    contact_no : $('#modify_contact_no').val(),
    email_id : $('#modify_email_id').val(),
    ssn : $('#modify_ssn').val(),
    dob : $('#modify_dob').val(),
    address : $('#modify_address').val()
  };

  fetch(homeURL+'modifyCustomer', {
    method : 'post',
    body : JSON.stringify(modify_customerData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("modify_customer response received");
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

  const deleteUserData = {
    userid: userid,
    customer_id: delete_id
  };

  fetch(homeURL+'deactivateCustomer', {
    method : 'post',
    body : JSON.stringify(deleteUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("deleteUser response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
  }).catch(function(error){
    console.error(error);
  });
}

function closeAccount(account_no) {
  console.log("closeAccount called");

  const closeAccountData = {
    userid: userid,
    account_no: account_no
  };

  fetch(homeURL+'deactivateAccount', {
    method : 'post',
    body : JSON.stringify(closeAccountData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("closeAccount response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Done'){
      window.alert('Account closed!');
    }
    else{
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
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

  fetch(homeURL+'resetpPassword', {
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

$(document).ready(function() {
  userid = localStorage.getItem('user');
  usertype = localStorage.getItem('usertype');

    $('#logout_btn').on('click', function(){
      logout();
    });
    $('#account_details_btn').on('click', function(){
      if($('#account_details_pane').css('display')=='none') {
        $('#account_details_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#transactions_btn').on('click', function(){
      if($('#transactions_pane').css('display')=='none') {
        $('#transactions_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','#FF6600');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#cust_accs_btn').on('click', function(){
      getUser();
      if($('#cust_accs_pane').css('display')=='none') {
        $('#cust_accs_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','#FF6600');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#app_dec_transactions_btn').on('click', function(){
      if($('#app_dec_transactions_pane').css('display')=='none') {
        $('#app_dec_transactions_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','#FF6600');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#create_accs_btn').on('click', function(){
      if($('#create_accs_pane').css('display')=='none') {
        $('#create_accs_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','#FF6600');
      $('#modify_accs_btn').css('background-color','maroon');
    });
    $('#modify_accs_btn').on('click', function(){
      if($('#modify_accs_pane').css('display')=='none') {
        $('#modify_accs_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
      $('#create_accs_btn').css('background-color','maroon');
      $('#modify_accs_btn').css('background-color','#FF6600');
    });
    $('#cust_accs_btn').click();
    $(".loader-wrapper").delay( 1000 ).fadeOut("slow");
    $('#home_logo').on('click', function(){
      $('#cust_accs_btn').click();
    });
    $('#update_info_btn').on('click', function(){
      console.log("updateInfo function");
      updateInfo(userid, $('#account_email_id').val(), $('#account_contact_no').val(), $('#account_address').val());
    });
    $('#customer_id_input_btn').on('click', function(){
      if($('#customer_id_input').val() == ''){
        window.alert('No input!');
      }
      else {
        getCustomer($('#customer_id_input').val());
      }
    });
    $('#modify_customer_account_get_btn').on('click', function(){
      if($('#modify_userid').val() == ''){
        window.alert('No input!');
      }
      else {
        getModifyCustomer($('#modify_userid').val());
      }
    });
    $('#customer_id_clear_btn').on('click', function(){
      $('#cust_details_card').hide();
      $('#cust_accounts_tbl').hide();
    });
    $('#approve_trans_btn').on('click', function(){
      if($('#customer_trans_no').val() == 'select'){
        window.alert('No input!');
      }
      else {
        approve_request(userid, $('#customer_trans_no').val());
      }
    });
    $('#deny_trans_btn').on('click', function(){
      if($('#customer_trans_no').val() == 'select'){
        window.alert('No input!');
      }
      else {
        deny_request(userid, $('#customer_trans_no').val());
      }
    });
    $('#create_customer_form').on('submit', function(e){
      e.preventDefault();
      register_customer();
    });
    $('#create_customer_account_btn').on('click', function(){
      if($('#account_customer_id').val() == ''){
        window.alert('No input!');
      }
      else {
        newAcc($('#account_customer_id').val(), $('#account_account_type').val());
      }
    });
    $('#modify_customer_form').on('submit', function(e){
      e.preventDefault();
      modify_customer();
    });
    $('#delete_customer_id_btn').on('click', function(){
      if($('#delete_customer_id').val() == ''){
        window.alert('No input!');
      }
      else {
        deleteUser(userid, $('#delete_customer_id').val());
      }
    });
    $('#close_account_no_btn').on('click', function(){
      if($('#close_account_no').val() == ''){
        window.alert('No input!');
      }
      else {
        closeAccount($('#close_account_no').val());
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
  });