//prevent browser back
history.pushState(null, null, document.URL);
window.addEventListener('popstate', function () {
    history.pushState(null, null, document.URL);
});

//prevent right-click
document.addEventListener("contextmenu", function(e){
  e.preventDefault();
})

const homeURL = 'https://www.cse545group6.online/';

var userid, usertype, firstname, midname, lastname, email, contact, dob, ssn, address;

function getUser() {
  console.log("gettier1 called");

  //userid = localStorage.getItem('user');
  console.log("userid retrieved from local storage ="+ userid);

  const loadUserData = {
    employee_id : userid,
    usertype : 'tier1'
  };

  fetch(homeURL+'loadEmployee', {
    method : 'post',
    body : JSON.stringify(loadUserData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("gettier1 response received");
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
  fillPendingTransTbl(data);
}

function fillPendingTransTbl(data){
  var table = document.getElementById('pending_transactions_tbl');
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

function fillPendingReqTbl(data){
  var table = document.getElementById('cust_reqs_tbl');
  var rowCount = table.rows.length;

  var selection = document.getElementById('customer_req_id');
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

function approveCustomerReq(userid, request_id) {
  console.log("approveCustomerReq called");

  const approveCustomerReqData = {
    userid : userid,
    update_req_no : request_id
  };

  fetch(homeURL+'approveUpdateInfo', {
    method : 'post',
    body : JSON.stringify(approveCustomerReqData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("approveCustomerReq response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function denyCustomerReq(userid, request_id) {
  console.log("denyCustomerReq called");

  const denyCustomerReqData = {
    userid : userid,
    update_req_no : request_id
  };

  fetch(homeURL+'denyUpdateInfo', {
    method : 'post',
    body : JSON.stringify(denyCustomerReqData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("denyCustomerReq response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    window.alert(data.message);
    getUser();
  }).catch(function(error){
    console.error(error);
  });
}

function order_check(userid, toAccount, fromAccount, amount) {
  console.log("getcashiercheck called");

  const orderCheckData = {
    userid : userid,
    to_account : toAccount,
    from_account : fromAccount,
    amount : amount
  };

  fetch(homeURL+'getCashierCheque', {
    method : 'post',
    body : JSON.stringify(orderCheckData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("orderCheck response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Success') {
      window.alert('Cheque Issued!');
    }
    else {
      window.alert(data.message );
    }
  }).catch(function(error){
    console.error(error);
  });
}

function dep_check(userid, checkno) {
  console.log("depositcheck called");

  const depCheckData = {
    userid : userid,
    cheque_no : checkno
  };

  fetch(homeURL+'depositCheck', {
    method : 'post',
    body : JSON.stringify(depCheckData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("depCheck response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Check already used'){
      window.alert('Cheque already used!');
    }
    if(data.message == 'Success') {
      window.alert('Cheque successfully deposited!');
    }
    else {
      window.alert('Failed! Please re-try.');
    }
  }).catch(function(error){
    console.error(error);
  });
}

function fund_transfer(userid, fromAccount, toAccount, amount) {
  console.log("fund transfer called");

  const fundTransferData = {
    userid : userid,
    fromAccount : fromAccount,
    toAccount : toAccount,
    amount : amount
  };

  fetch(homeURL+'fundTransfer', {
    method : 'post',
    body : JSON.stringify(fundTransferData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("fundTransfer response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'done'){
      window.alert('Successfully transferred!');
    }
    else if(data.message == 'Request to be approved by Tier2 employee'){
      window.alert(data.message);
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function deposit(userid, account, amount) {
  console.log("deposit called");

  const depositData = {
    userid : userid,
    account : account,
    amount : amount
  };

  fetch(homeURL+'depositAmount', {
    method : 'post',
    body : JSON.stringify(depositData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("deposit response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Success') {
      window.alert("Amount deposited!");
    }
    else {
      window.alert(data.message);
    }
  }).catch(function(error){
    console.error(error);
  });
}

function withdraw(userid, account, amount) {
  console.log("withdraw called");

  const withdrawData = {
    userid : userid,
    account : account,
    amount : amount
  };

  fetch(homeURL+'withdrawAmount', {
    method : 'post',
    body : JSON.stringify(withdrawData),
    headers : {
      'Content-type' : 'application/json'
    }
  }).then(function(response) {
    console.log("withdraw response received");
    return response.json();
  }).then(function (data) {
    console.log(data);
    if(data.message == 'Success') {
      window.alert("Amount withdrawn!");
    }
    else {
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
      if($('#account_details_pane').css('display')=='none'){
          $('#account_details_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#transactions_btn').on('click', function(){
      getUser();
      if($('#transactions_pane').css('display')=='none'){
          $('#transactions_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','#FF6600');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#app_dec_transactions_btn').on('click', function(){
      getUser();
      if($('#app_dec_transactions_pane').css('display')=='none'){
          $('#app_dec_transactions_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','#FF6600');
    });
    $('#cust_requests_btn').on('click', function(){
      if($('#cust_requests_pane').css('display')=='none'){
        $('#cust_requests_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','#FF6600');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#cust_accs_btn').on('click', function(){
      getUser();
      if($('#cust_accs_pane').css('display')=='none') {
        $('#cust_accs_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','#FF6600');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#app_dec_requests_btn').on('click', function(){
      if($('#app_dec_requests_pane').css('display')=='none') {
        $('#app_dec_requests_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','#FF6600');
      $('#cashier_cheques_btn').css('background-color','maroon');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
    });
    $('#cashier_cheques_btn').on('click', function(){
      if($('#cashier_cheques_pane').css('display')=='none') {
        $('#cashier_cheques_pane').show().siblings('div').hide();
      }
      $('#cust_accs_btn').css('background-color','maroon');
      $('#cust_requests_btn').css('background-color','maroon');
      $('#app_dec_requests_btn').css('background-color','maroon');
      $('#cashier_cheques_btn').css('background-color','#FF6600');
      $('#transactions_btn').css('background-color','maroon');
      $('#app_dec_transactions_btn').css('background-color','maroon');
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
    $('#customer_id_clear_btn').on('click', function(){
      $('#cust_details_card').hide();
      $('#cust_accounts_tbl').hide();
    });
    $('#approve_req_btn').on('click', function(){
      if($('#customer_req_id').val() == 'select'){
        window.alert('No input!');
      }
      else {
        approveCustomerReq(userid, $('#customer_req_id').val());
      }
    });
    $('#deny_req_btn').on('click', function(){
      if($('#customer_req_id').val() == 'select'){
        window.alert('No input!');
      }
      else {
        denyCustomerReq(userid, $('#customer_req_id').val());
      }
    });
    $('#issue_check_btn').on('click', function(){
      if($('#issue_check_from').val() == '' || $('#issue_check_to').val() == '' || $('#issue_check_amt').val() == ''){
        window.alert('Empty input!');
      }
      else {
        order_check(userid, $('#issue_check_to').val(), $('#issue_check_from').val(), $('#issue_check_amt').val());
      }
    });
    $('#deposit_check_btn').on('click', function(){
      if($('#deposit_check_no').val() == ''){
        window.alert('Empty input!');
      }
      else {
        dep_check(userid, $('#deposit_check_no').val());
      }
    });
    $('#transfer_btn').on('click', function(){
      if($('#transfer_from').val() == '' || $('#transfer_to').val() == '' || $('#transfer_amt').val() == ''){
        window.alert('Empty input!');
      }
      else {
        fund_transfer(userid, $('#transfer_from').val(), $('#transfer_to').val(), $('#transfer_amt').val());
      }
    });
    $('#deposit_btn').on('click', function(){
      if($('#deposit_to').val() == '' || $('#deposit_amt').val() == ''){
        window.alert('Empty input!');
      }
      else {
        deposit(userid, $('#deposit_to').val(), $('#deposit_amt').val());
      }
    });
    $('#withdraw_btn').on('click', function(){
      if($('#withdraw_from').val() == '' || $('#withdraw_amt').val() == ''){
        window.alert('Empty input!');
      }
      else {
        withdraw(userid, $('#withdraw_from').val(), $('#withdraw_amt').val());
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
  });
